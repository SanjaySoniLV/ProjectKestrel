import sys, json, pandas as pd, numpy as np
a=pd.read_csv(sys.argv[1]+'/.kestrel/kestrel_database.csv').set_index('filename'); b=pd.read_csv(sys.argv[2]+'/.kestrel/kestrel_database.csv').set_index('filename')
b=b.loc[a.index]
print("rows",len(a))
print("species agree", (a.species==b.species).sum(), "of", len(a))
print("family agree", (a.family==b.family).sum())
for c in ['species_confidence','quality','quality_siglip','quality_legacy']:
    d=(a[c]-b[c]).abs(); print(c,"max abs diff %.4f mean %.4f"%(d.max(),d.mean()))
print("star/quality rounded-to-0.1 differs:", (a.quality.round(1)!=b.quality.round(1)).sum())
def crops(x):
    try: return json.loads(x)
    except Exception: return []
nd=0; maxbox=0; ncrop_diff=0
for f in a.index:
    ca,cb=crops(a.loc[f,'crops_json']),crops(b.loc[f,'crops_json'])
    if len(ca)!=len(cb): ncrop_diff+=1; continue
print("frames with different crop count:",ncrop_diff)
da=a.detection_scores.astype(str); db=b.detection_scores.astype(str)
print("detection_scores identical strings:", (da==db).sum())
dif=a.index[(a.species!=b.species)]
print(pd.DataFrame({'dml':a.species[dif],'ov':b.species[dif],'cdml':a.species_confidence[dif],'cov':b.species_confidence[dif]}))
print("---- per-crop")
rd=[];dc=[];n=0;rat=0;sp=0
for f in a.index:
    ca,cb=crops(a.loc[f,'crops_json']),crops(b.loc[f,'crops_json'])
    for x,y in zip(ca,cb):
        n+=1; rat+= x.get('rating')==y.get('rating'); sp+= x.get('species')==y.get('species')
        dc.append(abs(x['detection_confidence']-y['detection_confidence'])); rd.append(abs((x.get('rating') or 0)-(y.get('rating') or 0)))
print("crops",n,"rating equal",rat,"species equal",sp,"max det conf diff %.4f"%max(dc),"max star diff",max(rd))
print([k for k in crops(a.iloc[0].crops_json)[0].keys()])
from PIL import Image
import os
dm=[]
for f in a.index:
    for x in crops(a.loc[f,'crops_json']):
        p=x['crop_path'].replace(chr(92),'/')
        pa=os.path.join(sys.argv[1],p); pb=os.path.join(sys.argv[2],p)
        ia,ib=Image.open(pa),Image.open(pb)
        if ia.size!=ib.size: dm.append((f,'size',ia.size,ib.size)); continue
        d=np.abs(np.asarray(ia,dtype=float)-np.asarray(ib,dtype=float)).mean(); dm.append((f,round(d,2)))
sz=[d for d in dm if d[1]=='size']; print("crops with different size:",len(sz), sz[:3]); v=[d[1] for d in dm if d[1]!='size']; print("mean abs pixel diff of same-size crops: max %.2f mean %.2f"%(max(v),np.mean(v)))
