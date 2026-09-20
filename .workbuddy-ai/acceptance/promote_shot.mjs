import { chromium } from "playwright-core";
const B="http://localhost:5174", T=process.env.API_TOKEN;
if(!T){console.error("API_TOKEN is unset. Run: python scripts/seed_admin_demo.py");process.exit(2);}
const C="C:/Program Files/Google/Chrome/Application/chrome.exe";
const OUT="D:/360Downloads/360驱动大师目录/b2b-ai-support-plan/b2b-ai-support-plan/.workbuddy-ai/acceptance/shots";
const br=await chromium.launch({executablePath:C,headless:true});
const c=await br.newContext({viewport:{width:1440,height:940}});
await c.addInitScript((t)=>{localStorage.setItem("b2b_token",t);localStorage.setItem("b2b_lang","en");},T);
c.setDefaultTimeout(8000); const p=await c.newPage();
await p.goto(B+"/prompts",{waitUntil:"domcontentloaded"}); await p.waitForTimeout(2000);
await p.locator("button",{hasText:/^Promote$/}).first().click();
await p.waitForTimeout(700);
await p.locator(".prompt .btn-primary").first().click();
await p.waitForTimeout(2500);
await p.screenshot({path:OUT+"/p0-promote-fake-success.png", fullPage:true});
console.log("banner=",await p.locator(".banner-ok").innerText().catch(()=>null));
await br.close();
