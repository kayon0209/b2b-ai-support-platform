import { chromium } from "playwright-core";
const B="http://localhost:5174", T=process.env.API_TOKEN;
if(!T){console.error("API_TOKEN is unset. Run: python scripts/seed_admin_demo.py");process.exit(2);}
const C="C:/Program Files/Google/Chrome/Application/chrome.exe";
const br=await chromium.launch({executablePath:C,headless:true});
const c=await br.newContext({viewport:{width:1440,height:940}});
await c.addInitScript((t)=>{localStorage.setItem("b2b_token",t);localStorage.setItem("b2b_lang","en");},T);
c.setDefaultTimeout(8000); const p=await c.newPage();
const net=[]; p.on("response",(r)=>{ if(r.url().includes("/v1/prompts")) net.push(r.status()+" "+r.request().method()+" "+new URL(r.url()).pathname); });
await p.goto(B+"/prompts",{waitUntil:"domcontentloaded"}); await p.waitForTimeout(2000);
const promoteBtn = p.locator("button",{hasText:/^Promote$/}).first();
console.log("promoteButton=",await promoteBtn.count());
await promoteBtn.click(); await p.waitForTimeout(700);
const confirmBtn = p.locator(".prompt .btn-primary").first();
await confirmBtn.click();
await p.waitForTimeout(2500);
console.log("banners=",JSON.stringify(await p.locator(".banner").allInnerTexts()));
console.log("net=",JSON.stringify(net));
await br.close();
