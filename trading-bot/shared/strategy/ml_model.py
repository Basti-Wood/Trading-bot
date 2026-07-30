"""
shared/strategy/ml_model.py — PyTorch LSTM (Prompt 2)

TradingLSTM: LSTM -> Linear(hidden->64) -> ReLU -> Dropout -> Linear(64->2)
Predicts price UP or DOWN in the next 5 candles.
Training on GPU (evolver), inference on CPU (bots).
No shuffling, no lookahead bias — time-series order is sacred.
"""

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader

from shared.evolution.dna import AgentDNA
from shared.data.preprocessor import FEATURES_BY_ASSET, BASE_FEATURES

PREDICT_HORIZON = 5   # candles ahead for the UP/DOWN target


class TradingLSTM(nn.Module):
    def __init__(self, input_size: int, hidden_size: int = 128,
                 num_layers: int = 2, dropout: float = 0.2):
        super().__init__()
        self.lstm = nn.LSTM(
            input_size=input_size,
            hidden_size=hidden_size,
            num_layers=num_layers,
            batch_first=True,
            dropout=dropout if num_layers > 1 else 0.0,
        )
        self.head = nn.Sequential(
            nn.Linear(hidden_size, 64),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(64, 2),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # x: (batch, seq_len, n_features) -> logits (batch, 2)
        out, _ = self.lstm(x)
        return self.head(out[:, -1, :])


class PriceSequenceDataset(Dataset):
    """
    Sliding window of `seq_len` candles per sample.
    Target: 1 if close.shift(-5) > close else 0.
    """

    def __init__(self, df: pd.DataFrame, feature_cols: list, seq_len: int):
        feats = df[feature_cols].to_numpy(dtype=np.float32)
        close = df["close"].to_numpy(dtype=np.float32)

        # Normalize features column-wise (z-score) using the WHOLE given df.
        # Caller must pass only training data for the training set to avoid
        # test-set leakage.
        mean = feats.mean(axis=0)
        std = feats.std(axis=0)
        std[std == 0] = 1.0
        self.mean, self.std = mean, std
        feats = (feats - mean) / std

        xs, ys = [], []
        limit = len(df) - seq_len - PREDICT_HORIZON
        for i in range(max(0, limit)):
            xs.append(feats[i:i + seq_len])
            future = close[i + seq_len - 1 + PREDICT_HORIZON]
            now = close[i + seq_len - 1]
            ys.append(1 if future > now else 0)
        self.x = np.array(xs, dtype=np.float32)
        self.y = np.array(ys, dtype=np.int64)

    def __len__(self) -> int:
        return len(self.x)

    def __getitem__(self, idx):
        return torch.from_numpy(self.x[idx]), int(self.y[idx])


def train_test_split_ts(df: pd.DataFrame, train_frac: float = 0.8):
    """Chronological 80/20 split — NEVER shuffled."""
    cut = int(len(df) * train_frac)
    return df.iloc[:cut], df.iloc[cut:]


def train_lstm(dna: AgentDNA, data, asset_class: str = "crypto",
               epochs: int = 50, batch_size: int = 64) -> TradingLSTM:
    """
    Phase 2: train ONE LSTM using the winning DNA.
    `data` may be a DataFrame with engineered features, or dict[symbol, df].
    """
    from shared.data.preprocessor import engineer_features

    feature_cols = FEATURES_BY_ASSET.get(asset_class, BASE_FEATURES)

    if isinstance(data, dict):
        frames = [engineer_features(df) for df in data.values()]
    else:
        frames = [engineer_features(data)]

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = TradingLSTM(
        input_size=len(feature_cols),
        hidden_size=dna.hidden_size,
        num_layers=dna.num_layers,
        dropout=dna.dropout,
    ).to(device)

    optimizer = torch.optim.Adam(model.parameters(), lr=1e-3, weight_decay=1e-4)
    criterion = nn.CrossEntropyLoss()

    # Build datasets per symbol, chronological split per frame
    train_sets, val_sets = [], []
    for df in frames:
        missing = [c for c in feature_cols if c not in df.columns]
        for col in missing:   # e.g. politician features unavailable offline
            df[col] = 0.0
        if len(df) < dna.seq_len + PREDICT_HORIZON + 10:
            continue
        train_df, val_df = train_test_split_ts(df)
        train_sets.append(PriceSequenceDataset(train_df, feature_cols, dna.seq_len))
        val_sets.append(PriceSequenceDataset(val_df, feature_cols, dna.seq_len))

    if not train_sets:
        raise ValueError("Not enough data to train LSTM")

    train_ds = torch.utils.data.ConcatDataset(train_sets)
    val_ds = torch.utils.data.ConcatDataset(val_sets)
    # shuffle=False — no data shuffling, ever
    train_loader = DataLoader(train_ds, batch_size=batch_size, shuffle=False)
    val_loader = DataLoader(val_ds, batch_size=batch_size, shuffle=False)

    for epoch in range(epochs):
        model.train()
        total_loss = 0.0
        for xb, yb in train_loader:
            xb, yb = xb.to(device), yb.to(device)
            optimizer.zero_grad()
            loss = criterion(model(xb), yb)
            loss.backward()
            optimizer.step()
            total_loss += loss.item()

        if (epoch + 1) % 10 == 0 or epoch == epochs - 1:
            model.eval()
            correct = total = 0
            with torch.no_grad():
                for xb, yb in val_loader:
                    preds = model(xb.to(device)).argmax(dim=1).cpu()
                    correct += int((preds == yb).sum())
                    total += len(yb)
            acc = correct / total if total else 0.0
            print(f"[lstm] epoch {epoch + 1}/{epochs} "
                  f"loss={total_loss / max(1, len(train_loader)):.4f} val_acc={acc:.3f}")

    model.cpu()   # inference runs on CPU
    return model


class MLPredictor:
    """
    Live inference wrapper. Maintains a rolling buffer of engineered feature
    rows; predicts once >= dna.seq_len rows are available.
    """

    def __init__(self, dna: AgentDNA, asset_class: str, model_path: str = None):
        self.dna = dna
        self.asset_class = asset_class
        self.feature_cols = FEATURES_BY_ASSET.get(asset_class, BASE_FEATURES)
        self.confidence_threshold = 0.80 - (dna.temperature * 0.28)
        self.buffer: list = []
        self.model: TradingLSTM | None = None
        # Exposed for dashboard visualization
        self.last_proba_up: float = 0.0
        self.last_proba_down: float = 0.0
        self.last_features: dict[str, float] = {}
        if model_path:
            self.load(model_path)

    def load(self, model_path: str) -> None:
        self.model = TradingLSTM(
            input_size=len(self.feature_cols),
            hidden_size=self.dna.hidden_size,
            num_layers=self.dna.num_layers,
            dropout=self.dna.dropout,
        )
        state = torch.load(model_path, map_location="cpu")
        self.model.load_state_dict(state)
        self.model.eval()

    def push(self, feature_row: dict) -> None:
        """Append the latest engineered feature row to the rolling buffer."""
        row = [float(feature_row.get(c, 0.0)) for c in self.feature_cols]
        self.buffer.append(row)
        max_keep = self.dna.seq_len * 3
        if len(self.buffer) > max_keep:
            self.buffer = self.buffer[-max_keep:]

    def predict(self) -> tuple:
        """
        Returns ('BUY' | 'SELL' | 'HOLD', confidence).
        HOLD if not enough candles, no model, or confidence below threshold.
        """
        if self.model is None or len(self.buffer) < self.dna.seq_len:
            return "HOLD", 0.0

        window = np.array(self.buffer[-self.dna.seq_len:], dtype=np.float32)
        # Normalize with the window's own stats (consistent with training z-score)
        mean, std = window.mean(axis=0), window.std(axis=0)
        std[std == 0] = 1.0
        window = (window - mean) / std

        x = torch.from_numpy(window).unsqueeze(0)   # (1, seq_len, features)
        with torch.no_grad():
            proba = torch.softmax(self.model(x), dim=1)[0]

        self.last_proba_down = float(proba[0])
        self.last_proba_up = float(proba[1])
        # Store last feature values for dashboard visualization
        if self.buffer:
            latest = self.buffer[-1]
            self.last_features = {
                self.feature_cols[i]: float(latest[i])
                for i in range(min(len(self.feature_cols), len(latest)))
            }

        conf = float(proba.max())
        if conf < self.confidence_threshold:
            return "HOLD", conf
        return ("BUY" if proba[1] > proba[0] else "SELL"), conf
