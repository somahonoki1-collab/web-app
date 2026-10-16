"""Read public research supplements; cache them outside the application.

This script does not access the app database or change application code.
Run with the bundled Python runtime. Downloads use only public source URLs.
"""
import concurrent.futures
import hashlib
import html
import io
import json
import re
import tempfile
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

CACHE = Path(tempfile.gettempdir()) / "web_app_growth_research_20261007"
CACHE.mkdir(exist_ok=True)
ARTICLE = "https://link.springer.com/article/10.1186/s40798-025-00903-z"
SUPPLEMENT = "https://media.springernature.com/original/springer-static/esm/art%3A10.1186%2Fs40798-025-00903-z/MediaObjects/40798_2025_903_MOESM6_ESM.xlsx"


def download(label, url):
    target = CACHE / label
    if target.exists():
        data = target.read_bytes()
    else:
        request = urllib.request.Request(url, headers={"User-Agent": "Research-source-reader/1.0"})
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read()
        target.write_bytes(data)
    return data


def plain_html(content):
    content = re.sub(r"<(script|style)\b.*?</\1>", "", content, flags=re.S)
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", content)).split())


def inspect_table(label, url):
    content = download(label, url).decode()
    tables = re.findall(r"<table\b.*?</table>", content, flags=re.S)
    return {"source": url, "tables": [plain_html(table) for table in tables]}


def inspect_osf(node):
    url = f"https://api.osf.io/v2/nodes/{node}/files/osfstorage/?page[size]=100"
    result = json.loads(download(f"osf_{node}.json", url))
    return {"osf": node, "files": [
        {"name": x["attributes"]["name"], "kind": x["attributes"]["kind"],
         "download": x["links"].get("download"),
         "children": x.get("relationships", {}).get("files", {}).get("links", {}).get("related", {})}
        for x in result.get("data", [])], "next": result.get("links", {}).get("next")}


def inspect_xlsx():
    raw = download("ferrari_predictor.xlsx", SUPPLEMENT)
    ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        strings = []
        if "xl/sharedStrings.xml" in z.namelist():
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            strings = ["".join(t.text or "" for t in row.findall(".//s:t", ns)) for row in root]
        sheets = []
        for name in z.namelist():
            if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name):
                root = ET.fromstring(z.read(name))
                rows = []
                for row in root.findall(".//s:sheetData/s:row", ns):
                    cells = []
                    for cell in row:
                        v = cell.find("s:v", ns)
                        value = v.text if v is not None else "".join(t.text or "" for t in cell.findall(".//s:t", ns))
                        if cell.get("t") == "s":
                            value = strings[int(value)]
                        f = cell.find("s:f", ns)
                        if value or f is not None:
                            cells.append({"cell": cell.get("r"), "value": value, "formula": f.text if f is not None else None})
                    if cells:
                        rows.append(cells)
                sheets.append({"sheet": name, "rows": rows})
    result = {"source": SUPPLEMENT, "sha256": hashlib.sha256(raw).hexdigest(), "sheets": sheets}
    (CACHE / "ferrari_cells.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    jobs = {
        "bench_coefficients": lambda: inspect_table("table6.html", ARTICLE + "/tables/6"),
        "model_errors": lambda: inspect_table("table9.html", ARTICLE + "/tables/9"),
        "published_calculator": inspect_xlsx,
    }
    for node in ("jn6ay", "ruwda", "23qth", "6z3xu"):
        jobs[f"osf_{node}"] = lambda node=node: inspect_osf(node)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(fn): key for key, fn in jobs.items()}
        for future in concurrent.futures.as_completed(futures):
            key = futures[future]
            try:
                value = future.result()
                (CACHE / f"{key}_inspection.json").write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
                if key == "published_calculator":
                    value = {"source": value["source"], "sha256": value["sha256"], "sheet_sizes": [(s["sheet"], len(s["rows"])) for s in value["sheets"]]}
                print(json.dumps({"label": key, "result": value}, ensure_ascii=True))
            except Exception as exc:
                print(json.dumps({"label": key, "error": str(exc)}, ensure_ascii=True))
    print("CACHE", CACHE)
