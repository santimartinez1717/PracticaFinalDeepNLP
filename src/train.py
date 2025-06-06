import os
import numpy as np
import pandas as pd
from tqdm.auto import tqdm
import torch
from torch.utils.tensorboard import SummaryWriter
from torch.optim.lr_scheduler import OneCycleLR

from src.models import NNCRF
from src.utils import load_umt_loaders, Accuracy, save_model, set_seed, F1Score

from src.config import Config

os.environ["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"

device = torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")
if torch.cuda.is_available():
    torch.cuda.empty_cache()
print(device)

# Configuración global optimizada
set_seed(42)
torch.set_num_threads(8)

# Hyperparameters optimized for multi-task learning
EPOCHS = 30  # Increased for better convergence
BATCH_SIZE = 64  # Decreased for better generalization
LEARNING_RATE = 1e-3  # Better default for AdamW
WEIGHT_DECAY = 0.05  # Increased for better regularization
GRAD_CLIP = 1.0  # Keep gradient clipping
GRAD_ACCUM_STEPS = 2  # Gradient accumulation (effective batch: 64*2=128)
PATIENCE = 3  # Early stopping patience
SCHEDULER_PCT_START = 0.3  # Percent of training spent increasing LR
MAX_LR_FACTOR = 10  # Maximum LR will be LEARNING_RATE * MAX_LR_FACTOR

# Additional hyperparameters for task-specific early stopping
PATIENCE_NER = 5  # Patience for NER task
PATIENCE_SA = 5  # Patience for SA task
MIN_DELTA = 0.001  # Minimum improvement required


def train(model, optimizer, train_loader, device, epoch, config, scaler=None):
    model.train()
    train_loss = 0.0

    # Métricas para NER
    accuracy_ner = Accuracy()
    f1_ner = F1Score()

    # Métricas para SA
    accuracy_sa = Accuracy()
    f1_sa = F1Score()

    # Progress bar
    progress_bar = tqdm(train_loader, desc=f"Epoch {epoch}/{EPOCHS}")

    # Track for gradient accumulation
    accum_iter = 0

    for batch_idx, (
        word_seq_tensor,
        seq_lens,
        label_tensor,
        char_inputs,
        char_lens,
        edge_index,
        sentiment_labels,
    ) in enumerate(progress_bar):
        # Zero gradients at the right interval for gradient accumulation
        if accum_iter == 0:
            optimizer.zero_grad()

        # Normal forward pass (sin autocast)
        loss, preds_ner, preds_sa = model(
            word_seq_tensor,
            char_inputs,
            char_lens,
            edge_index,
            seq_lens,
            tags=label_tensor,
            sentiment_labels=sentiment_labels,
        )

        # Scale loss for gradient accumulation
        loss = loss / GRAD_ACCUM_STEPS

        # Normal backward pass (sin scaler)
        loss.backward()

        accum_iter += 1
        train_loss += loss.item() * GRAD_ACCUM_STEPS  # Undo scaling for logging

        # Update weights when we reach accumulation steps or at the last batch
        if accum_iter == GRAD_ACCUM_STEPS or batch_idx == len(train_loader) - 1:
            # Gradient clipping
            torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=GRAD_CLIP)

            # Normal optimizer step (sin scaler)
            optimizer.step()

            accum_iter = 0

        # Calculate metrics for NER
        flat_preds_ner = []
        for pred_seq, length in zip(preds_ner, seq_lens):
            if isinstance(pred_seq, torch.Tensor):
                flat_preds_ner.extend(pred_seq[:length].tolist())
            else:
                flat_preds_ner.extend(pred_seq[:length])
        flat_preds_ner = torch.tensor(flat_preds_ner, device=device)

        flat_labels_ner = label_tensor.view(-1)
        mask_ner = flat_labels_ner != config.label2idx[config.PAD]
        accuracy_ner.update(flat_preds_ner, flat_labels_ner[mask_ner])
        f1_ner.update(flat_preds_ner, flat_labels_ner[mask_ner])

        # Calculate metrics for SA
        accuracy_sa.update(preds_sa, sentiment_labels)
        f1_sa.update(preds_sa, sentiment_labels)

    # Calculate metrics for the epoch
    train_acc_ner = accuracy_ner.compute()
    train_f1_ner = f1_ner.compute()
    train_acc_sa = accuracy_sa.compute()
    train_f1_sa = f1_sa.compute()

    return train_loss / len(train_loader), {
        "train_acc_ner": train_acc_ner,
        "train_f1_ner": train_f1_ner,
        "train_acc_sa": train_acc_sa,
        "train_f1_sa": train_f1_sa,
    }


def validate(model, val_loader, device, config):
    model.eval()
    val_loss = 0.0

    # Métricas para NER
    accuracy_ner = Accuracy()
    f1_ner = F1Score()

    # Métricas para SA
    accuracy_sa = Accuracy()
    f1_sa = F1Score()

    with torch.no_grad():
        for (
            word_seq_tensor,
            seq_lens,
            label_tensor,
            char_inputs,
            char_lens,
            edge_index,
            sentiment_labels,
        ) in tqdm(val_loader, desc="Validating"):
            # Forward pass
            loss_value, preds_ner, preds_sa = model(
                word_seq_tensor,
                char_inputs,
                char_lens,
                edge_index,
                seq_lens,
                tags=label_tensor,
                sentiment_labels=sentiment_labels,
            )

            val_loss += loss_value.item()

            # Metrics calculation
            flat_preds_ner = []
            for pred_seq, length in zip(preds_ner, seq_lens):
                if isinstance(pred_seq, torch.Tensor):
                    flat_preds_ner.extend(pred_seq[:length].tolist())
                else:
                    flat_preds_ner.extend(pred_seq[:length])
            flat_preds_ner = torch.tensor(flat_preds_ner, device=device)

            flat_labels_ner = label_tensor.view(-1)
            mask_ner = flat_labels_ner != config.label2idx[config.PAD]
            accuracy_ner.update(flat_preds_ner, flat_labels_ner[mask_ner])
            f1_ner.update(flat_preds_ner, flat_labels_ner[mask_ner])

            accuracy_sa.update(preds_sa, sentiment_labels)
            f1_sa.update(preds_sa, sentiment_labels)

    val_acc_ner = accuracy_ner.compute()
    val_f1_ner = f1_ner.compute()
    val_acc_sa = accuracy_sa.compute()
    val_f1_sa = f1_sa.compute()

    return val_loss / len(val_loader), {
        "val_acc_ner": val_acc_ner,
        "val_f1_ner": val_f1_ner,
        "val_acc_sa": val_acc_sa,
        "val_f1_sa": val_f1_sa,
    }


def main():
    results = []
    config = Config(device=device)
    train_loader, val_loader, test_loader = load_umt_loaders(
        config, batch_size=BATCH_SIZE
    )

    print(
        f"\n====== Training with ReduceLROnPlateau LR (base_lr={LEARNING_RATE}, max_lr={LEARNING_RATE*MAX_LR_FACTOR}) ======"
    )

    model = NNCRF(config).to(device)

    # Separate parameters for SA and other tasks
    sa_params = [param for name, param in model.named_parameters() if "sa" in name]
    other_params = [
        param for name, param in model.named_parameters() if "sa" not in name
    ]

    # Optimizer with weight decay
    optimizer = torch.optim.AdamW(
        [
            {
                "params": sa_params,
                "lr": LEARNING_RATE * 3,
                "weight_decay": WEIGHT_DECAY,
            },
            {"params": other_params, "lr": LEARNING_RATE, "weight_decay": WEIGHT_DECAY},
        ],
        betas=(0.9, 0.999),
        eps=1e-8,
    )

    # Calculate total steps and warmup steps
    total_steps = len(train_loader) * EPOCHS // GRAD_ACCUM_STEPS

    # Alternativa: ReduceLROnPlateau - reduce LR cuando la métrica se estanca
    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
        optimizer, mode="max", factor=0.5, patience=2, min_lr=1e-6, verbose=True
    )

    # Setup tensorboard
    writer = SummaryWriter(
        log_dir=f"runs/ner_sa_reduce_lr_on_plateau_{LEARNING_RATE}_wd{WEIGHT_DECAY}"
    )

    # Early stopping variables - separate for each task
    best_val_f1_ner = 0.0
    best_val_f1_sa = 0.0
    best_epoch_ner = 0
    best_epoch_sa = 0
    patience_counter_ner = 0
    patience_counter_sa = 0

    for epoch in range(1, EPOCHS + 1):
        print(f"\nEpoch {epoch}/{EPOCHS}")

        # Training phase
        train_loss, train_metrics = train(
            model, optimizer, train_loader, device, epoch, config
        )

        # Validation phase
        val_loss, val_metrics = validate(model, val_loader, device, config)

        # Actualizar scheduler basado en la métrica de validación
        scheduler.step(val_metrics["val_f1_sa"])

        # ====== AÑADIR AQUÍ - Mostrar y registrar pérdidas ======
        # Mostrar pérdidas en consola
        print(f"Loss - Train: {train_loss:.4f} | Val: {val_loss:.4f}")

        # Registrar pérdidas en TensorBoard
        writer.add_scalar("Loss/train", train_loss, epoch)
        writer.add_scalar("Loss/val", val_loss, epoch)

        # Task-specific early stopping check for NER
        if val_metrics["val_f1_ner"] > best_val_f1_ner + MIN_DELTA:
            best_val_f1_ner = val_metrics["val_f1_ner"]
            best_epoch_ner = epoch
            patience_counter_ner = 0
            
        else:
            patience_counter_ner += 1
            print(
                f"NER not improved for {patience_counter_ner} epochs. Best F1: {best_val_f1_ner:.4f}"
            )

        # Task-specific early stopping check for SA
        if val_metrics["val_f1_sa"] > best_val_f1_sa + MIN_DELTA:
            best_val_f1_sa = val_metrics["val_f1_sa"]
            best_epoch_sa = epoch
            patience_counter_sa = 0
            
        else:
            patience_counter_sa += 1
            print(
                f"SA not improved for {patience_counter_sa} epochs. Best F1: {best_val_f1_sa:.4f}"
            )

        # Also save a combined model when both metrics improve
        if (
            val_metrics["val_f1_ner"] >= best_val_f1_ner
            and val_metrics["val_f1_sa"] >= best_val_f1_sa
        ):
            save_model(model, config, "combined_best_model")
            print("New best combined model saved!")

        # Check early stopping criteria
        ner_stopping = patience_counter_ner >= PATIENCE_NER
        sa_stopping = patience_counter_sa >= PATIENCE_SA

        # Log detailed information about current status
        print(
            f"Train - NER: {train_metrics['train_f1_ner']:.4f} | SA: {train_metrics['train_f1_sa']:.4f}"
        )
        print(
            f"Val   - NER: {val_metrics['val_f1_ner']:.4f} | SA: {val_metrics['val_f1_sa']:.4f}"
        )
        print(
            f"Best  - NER: {best_val_f1_ner:.4f} (epoch {best_epoch_ner}) | SA: {best_val_f1_sa:.4f} (epoch {best_epoch_sa})"
        )

        # Determine which task(s) triggered early stopping
        early_stop_reasons = []
        if ner_stopping:
            early_stop_reasons.append("NER")
        if sa_stopping:
            early_stop_reasons.append("SA")

        # Apply early stopping if either task meets the criterion
        if ner_stopping or sa_stopping:
            stop_reason = " & ".join(early_stop_reasons)
            print(
                f"\nEarly stopping triggered at epoch {epoch}! Tasks that plateaued: {stop_reason}"
            )
            print(
                f"Best performance - NER F1: {best_val_f1_ner:.4f} (epoch {best_epoch_ner})"
            )
            print(
                f"Best performance - SA F1: {best_val_f1_sa:.4f} (epoch {best_epoch_sa})"
            )
            break

    writer.close()
    results.append({"learning_rate": LEARNING_RATE, "val_acc": val_metrics})

    df = pd.DataFrame(results)
    os.makedirs("results", exist_ok=True)
    df.to_csv("results/experiment_onecycle_results.csv", index=False)


if __name__ == "__main__":
    main()
