"""Original deterministic score for the support film. No external samples/models."""
import json,math,wave,array
from pathlib import Path
plan=json.loads(Path('plan.json').read_text());sr=48000;dur=plan['duration'];n=round(sr*dur)
l=array.array('f',[0])*n;r=array.array('f',[0])*n

def note(at,length,midi,amp=.08,pan=0,kind='pluck'):
 f=440*2**((midi-69)/12);start=round(at*sr);count=min(round(length*sr),n-start)
 if start<0 or count<1:return
 for i in range(count):
  t=i/sr;u=t/length
  env=(1-math.exp(-t*35))*math.exp(-t*3.8) if kind=='pluck' else min(1,t/.3)*min(1,(length-t)/.6)*.55
  tone=math.sin(2*math.pi*f*t)+.18*math.sin(2*math.pi*f*2*t)+.07*math.sin(2*math.pi*f*3*t)
  v=amp*env*tone;l[start+i]+=v*(1-pan*.3);r[start+i]+=v*(1+pan*.3)
# Harmonic changes are tied to actual shots; rests keep UI reading uncluttered.
chords=[[50,57,62,65],[46,53,58,62],[50,57,62,69],[45,52,57,64],[53,60,65,69]]
for idx,s in enumerate(plan['shots']):
 length=s['end']-s['start']
 for j,m in enumerate(chords[idx]):note(s['start'],length,m,.075,(j-1.5)/2,'pad')
 if idx in (1,2,3):
  for k in range(int(length/.5)-1):
   if k%4==3:continue
   note(s['start']+.5+k*.5,.8,chords[idx][k%4]+12,.055,(-1)**k*.6)
 if idx in (1,2,3):
  for k in range(int(length)):
   st=round((s['start']+k+.15)*sr)
   for i in range(min(int(.18*sr),n-st)):
    t=i/sr;v=.07*math.exp(-t*22)*math.sin(2*math.pi*(58*t+5*(1-math.exp(-t*28))/28));l[st+i]+=v;r[st+i]+=v
# Tail settles to F with sparse descending notes.
for k,m in enumerate([77,72,69,65]):note(plan['shots'][-1]['start']+.3+k*.55,1.4,m,.06,(-1)**k*.35)
output=array.array('h')
for i in range(n):
 t=i/sr;fade=min(1,t/.4,(dur-t)/1.25)
 for ch in (l,r):output.append(round(32767*max(-.9,min(.9,ch[i]*fade))))
Path('assets').mkdir(exist_ok=True)
with wave.open('assets/music.wav','wb') as w:w.setnchannels(2);w.setsampwidth(2);w.setframerate(sr);w.writeframes(output.tobytes())
print('original music:',dur,'seconds')
