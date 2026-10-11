"""Executes cctv-analysis.ipynb outside Colab against local test data (CPU),
with a tiny VLM so the whole code path, including AI descriptions, is exercised."""
import os, sys, nbformat
from nbclient import NotebookClient
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
nb = nbformat.read(os.path.join(ROOT, "cctv-analysis.ipynb"), as_version=4)
inp, out = sys.argv[1], sys.argv[2]
for c in nb.cells:
    if c.cell_type != "code":
        continue
    s = c.source
    s = s.replace('/content/drive/MyDrive/CCTV_Analysis', out).replace('/content/drive/MyDrive/CCTV_known_faces',
                  os.path.join(ROOT, "test_data/known_faces")).replace('"/content/drive/MyDrive/CCTV"', f'"{inp}"')
    s = s.replace('/content/cctv_work', out + "_work").replace("/content/cctv_analysis", out + "_pkg/cctv_analysis")
    s = s.replace("sys.path.insert(0, '/content')", f"sys.path.insert(0, '{out}_pkg')").replace("'/content' not in sys.path", f"'{out}_pkg' not in sys.path")
    s = s.replace('SPEED_PRESET = "balanced"', 'SPEED_PRESET = "fast"').replace('AI_DESCRIBE = "flagged"', 'AI_DESCRIBE = "people"')
    s = s.replace('EXTRA = {}', 'EXTRA = {"vlm_model": "HuggingFaceTB/SmolVLM-256M-Instruct", "vlm_max_new_tokens": 60, "det_imgsz": 640}')
    s = s.replace('/content/drive/MyDrive/suspect.jpg', os.path.join(ROOT, "test_data/lena.jpg"))
    if s.startswith("!pip") or "!pip -q install" in s:
        s = "print('skip install')"
    c.source = s
NotebookClient(nb, timeout=3600, kernel_name="python3", resources={"metadata": {"path": ROOT}}).execute()
nbformat.write(nb, out + "_executed.ipynb")
errs = [o for c in nb.cells if c.cell_type == "code" for o in c.get("outputs", []) if o.get("output_type") == "error"]
print("errors:", len(errs))
for e in errs:
    print(e["ename"], e["evalue"])
