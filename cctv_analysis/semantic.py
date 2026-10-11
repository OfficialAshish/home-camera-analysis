"""SigLIP-2 zero-shot scene tagging of keyframes + an embedding index that
powers free-text search ("person in red shirt near counter")."""
from __future__ import annotations

import logging
import os
from typing import List, Tuple

import numpy as np

log = logging.getLogger("cctv")


class Semantic:
    def __init__(self, cfg, device: str):
        import torch
        from transformers import AutoModel, AutoProcessor
        self.torch = torch
        self.device = device if device.startswith("cuda") else "cpu"
        dtype = torch.float16 if self.device.startswith("cuda") else torch.float32
        from . import modelcache
        cd = modelcache.hf_dir()
        self.model = AutoModel.from_pretrained(cfg.semantic_model, dtype=dtype, cache_dir=cd).to(self.device).eval()
        self.proc = AutoProcessor.from_pretrained(cfg.semantic_model, cache_dir=cd)
        self.dtype = dtype
        self.sus = list(cfg.suspicious_prompts)
        self.norm = list(cfg.normal_prompts)
        self.prompts = self.sus + self.norm
        self.text_emb = self.encode_text([f"this is a photo of {x}." for x in self.prompts])
        self.scale = float(self.model.logit_scale.exp().item())
        self.bias = float(self.model.logit_bias.item()) if hasattr(self.model, "logit_bias") else 0.0

    def encode_text(self, texts: List[str]) -> np.ndarray:
        with self.torch.no_grad():
            t = self.proc(text=[x.lower() for x in texts], padding="max_length", max_length=64,
                          truncation=True, return_tensors="pt").to(self.device)
            e = self.model.get_text_features(**t)
            if not isinstance(e, self.torch.Tensor):
                e = getattr(e, "pooler_output", e[0])
            e = e.float()
            e = e / e.norm(dim=-1, keepdim=True)
        return e.cpu().numpy()

    def encode_images(self, images_bgr: List[np.ndarray], batch: int = 16) -> np.ndarray:
        from PIL import Image
        outs = []
        for i in range(0, len(images_bgr), batch):
            ims = [Image.fromarray(im[:, :, ::-1]) for im in images_bgr[i:i + batch]]
            with self.torch.no_grad():
                x = self.proc(images=ims, return_tensors="pt").to(self.device)
                x = {k: (v.to(self.dtype) if v.dtype.is_floating_point else v) for k, v in x.items()}
                e = self.model.get_image_features(**x)
                if not isinstance(e, self.torch.Tensor):
                    e = getattr(e, "pooler_output", e[0])
                e = e.float()
                e = e / e.norm(dim=-1, keepdim=True)
            outs.append(e.cpu().numpy())
        return np.concatenate(outs) if outs else np.zeros((0, self.text_emb.shape[1]), np.float32)

    def classify(self, img_emb: np.ndarray) -> List[dict]:
        """SigLIP's native per-prompt sigmoid scores (well calibrated: ~0 when no
        prompt fits). 'lift' compares the best suspicious vs best normal prompt."""
        res = []
        logits = img_emb @ self.text_emb.T * self.scale + self.bias
        sig = 1 / (1 + np.exp(-logits))
        ns = len(self.sus)
        for row in sig:
            top = int(np.argmax(row))
            ts = int(np.argmax(row[:ns]))
            tn = ns + int(np.argmax(row[ns:])) if len(row) > ns else None
            best_n = float(row[tn]) if tn is not None else 0.0
            lift = float(row[ts] / (row[ts] + best_n + 1e-9))
            res.append({"top_label": self.prompts[top], "top_score": float(row[top]),
                        "top_suspicious": self.sus[ts], "suspicious_score": float(row[ts]),
                        "top_normal": self.prompts[tn] if tn is not None else "", "normal_score": best_n,
                        "lift": lift})
        return res


def search_index(sem: Semantic, index_path: str, query: str, k: int = 20) -> List[Tuple[str, float]]:
    """Rank stored keyframes by similarity to a free-text query."""
    if not os.path.exists(index_path):
        return []
    z = np.load(index_path, allow_pickle=True)
    emb, keys = z["emb"].astype(np.float32), z["keys"]
    q = sem.encode_text([f"this is a photo of {query}."])[0]
    s = emb @ q
    order = np.argsort(-s)[:k]
    return [(str(keys[i]), float(s[i])) for i in order]
