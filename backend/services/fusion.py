from typing import Dict, List, Any

ALIASES = {
    "项目名": "项目名称", "课题名称": "项目名称", "联系人": "负责人",
    "对接人": "负责人", "联系方式": "电话", "手机号": "电话",
    "邮件": "邮箱", "email": "邮箱", "单位": "单位名称",
    "公司": "单位名称", "金额": "合同金额", "签约日期": "合同日期",
}

SOURCE_W = {"table": 0.95, "paragraph": 0.85, "title": 0.80}


def normalize(name: str) -> str:
    return ALIASES.get(name, name)


def fuse(extractions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    candidates: Dict[str, List[Dict]] = {}
    for ext in extractions:
        fid, fn, st = ext["file_id"], ext["file_name"], ext.get("source_type", "paragraph")
        w = SOURCE_W.get(st, 0.80)
        for raw, val in ext["fields"].items():
            norm = normalize(raw)
            v = str(val).strip() if val else ""
            if not v:
                continue
            candidates.setdefault(norm, []).append({"value": v, "weight": w, "file_id": fid, "file_name": fn, "source_type": st})
    results = []
    for field, cands in candidates.items():
        groups: Dict[str, List] = {}
        for c in cands:
            groups.setdefault(c["value"], []).append(c)
        best_val, best_score, best_c = "", 0.0, cands[0]
        for val, grp in groups.items():
            s = max(c["weight"] for c in grp) + min(len(grp) * 0.05, 0.15)
            if s > best_score:
                best_score, best_val = s, val
                best_c = max(grp, key=lambda x: x["weight"])
        results.append({
            "field_name": field, "field_value": best_val,
            "confidence": round(min(best_score, 0.99), 4),
            "source_file": best_c["file_name"], "source_type": best_c["source_type"],
            "candidates_count": len(cands),
            "all_candidates": [{"value": c["value"], "file": c["file_name"], "weight": c["weight"]} for c in cands],
        })
    return results
