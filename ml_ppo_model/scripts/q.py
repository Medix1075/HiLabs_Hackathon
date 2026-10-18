import json, requests, sys
KEY=next(r for r in json.load(open("reqs.json")) if "pces" in r["url"])["headers"]["x-api-key"]  # saved by probe.py
H={"x-api-key":KEY,"content-type":"application/json; charset=UTF-8","accept":"application/json","referer":"https://findaprovider.healthnetcalifornia.com/","origin":"https://findaprovider.healthnetcalifornia.com"}
def q(body):
    r=requests.post("https://external-api.search.my.centene.com/pces/query",json=body,headers=H,timeout=60)
    r.raise_for_status(); return r.json()
if __name__=="__main__":
    npi=sys.argv[1]
    for field in ["nationalProviderIdentifiers","nationalProviderIdentifiers.npi","npi"]:
        try:
            d=q({"customQuery":{"values":{"field":field,"values":[npi]}},"size":50,"from":0})
        except Exception as e: print(field,"ERR",e); continue
        hits=d.get("hits") or d.get("results") or d
        print(field, type(d), list(d.keys())[:10] if isinstance(d,dict) else '')
        print(json.dumps(d)[:600]); print()
