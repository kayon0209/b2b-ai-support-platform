import { chromium } from "playwright-core";
const B="http://localhost:5174", T=process.env.API_TOKEN;
if(!T){console.error("API_TOKEN is unset. Run: python scripts/seed_admin_demo.py");process.exit(2);}
const C="C:/Program Files/Google/Chrome/Application/chrome.exe";
const br=await chromium.launch({executablePath:C,headless:true});
const c=await br.newContext({viewport:{width:1440,height:940}});
await c.addInitScript((t)=>{localStorage.setItem("b2b_token",t);localStorage.setItem("b2b_lang","zh");},T);
c.setDefaultTimeout(8000);
const p=await c.newPage();
for(const r of ["/quality","/gaps","/prompts","/flags","/cases","/members","/usage","/branding"]){
  await p.goto(B+r,{waitUntil:"domcontentloaded"}); await p.waitForTimeout(1600);
  const nav=await p.locator(".nav-item span:last-child").allInnerTexts();
  const h1=await p.locator("h1").allInnerTexts();
  const sub=await p.locator(".page-header p").allInnerTexts();
  const zh=(nav.join("")+h1.join("")+sub.join("")).match(/[\u4e00-\u9fff]/g)?.length||0;
  const en=(nav.join("")+h1.join("")+sub.join("")).match(/[A-Za-z]/g)?.length||0;
  console.log(r.padEnd(10), "zhChars="+zh, "enChars="+en, "| nav[0]="+JSON.stringify(nav[0]), "| h1="+JSON.stringify(h1[0]), "| sub="+JSON.stringify(sub[0]));
}
await br.close();
