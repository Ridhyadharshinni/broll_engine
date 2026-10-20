#!/usr/bin/env python3
"""Run JSON request cases against a running B-roll API and save all evidence."""
import argparse, json, time, uuid
from pathlib import Path
import requests

def main():
    p=argparse.ArgumentParser(); p.add_argument("input", help="JSON file or directory of JSON requests"); p.add_argument("--base-url",default="http://localhost:8000"); p.add_argument("--output",default="test-results"); p.add_argument("--timeout",type=int,default=900); args=p.parse_args()
    source=Path(args.input); files=[source] if source.is_file() else sorted(source.glob("*.json")); out=Path(args.output); out.mkdir(parents=True,exist_ok=True)
    summary=[]
    for f in files:
        case=out/f.stem; case.mkdir(exist_ok=True)
        payload=json.loads(f.read_text(encoding="utf-8")); (case/"request.json").write_text(json.dumps(payload,indent=2),encoding="utf-8")
        try:
            r=requests.post(args.base_url+"/api/v1/broll/jobs",json=payload,headers={"Idempotency-Key":str(uuid.uuid4()),"X-Correlation-Id":str(uuid.uuid4())},timeout=30); (case/"submit.json").write_text(r.text,encoding="utf-8"); r.raise_for_status(); job=r.json()["job_id"]
            start=time.time(); final=None
            while time.time()-start<args.timeout:
                rr=requests.get(args.base_url+f"/api/v1/jobs/{job}",timeout=20); final=rr.json(); (case/"status.json").write_text(json.dumps(final,indent=2),encoding="utf-8")
                if final.get("status") in ("succeeded","failed"): break
                time.sleep(2)
            if final and final.get("status")=="succeeded":
                result=final.get("result",{})
                for label,url in [("timeline",result.get("timeline")),("manifest",result.get("manifest")),("captions",result.get("captions"))]:
                    if url:
                        q=requests.get(args.base_url+url,timeout=30)
                        if q.ok: (case/f"{label}{Path(url).suffix or '.txt'}").write_bytes(q.content)
                for ratio,url in result.get("videos",{}).items():
                    q=requests.get(args.base_url+url,timeout=180)
                    if q.ok: (case/f"video_{ratio.replace(':','x')}.mp4").write_bytes(q.content)
            summary.append({"input":str(f),"job_id":job,"status":(final or {}).get("status","timeout"),"elapsed_seconds":round(time.time()-start,2)})
        except Exception as e: summary.append({"input":str(f),"status":"error","error":str(e)})
    (out/"summary.json").write_text(json.dumps(summary,indent=2),encoding="utf-8"); print(json.dumps(summary,indent=2))
if __name__=="__main__": main()
