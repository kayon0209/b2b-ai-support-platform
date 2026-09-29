import {API_FIXTURES,API_PREFIXES,NOW} from './fixtures/api.js';
const RealDate=Date;class FilmDate extends RealDate{constructor(...a){a.length?super(...a):super(NOW*1000)}static now(){return NOW*1000}}globalThis.Date=FilmDate;
let seed=7418;Math.random=()=>{seed=seed*16807%2147483647;return(seed-1)/2147483646};
localStorage.clear();sessionStorage.clear();localStorage.setItem('b2b_lang','zh');
history.replaceState(null,'','?tenant=film-demo');
export function resetCustomerSession(){sessionStorage.setItem('support.visitor.v1',JSON.stringify('film-visitor'));sessionStorage.setItem('support.session.v1',JSON.stringify({token:'synthetic-fixture-not-a-credential',conversation_ref:'film-conversation',expires_at:NOW+86400,visitor_id:'film-visitor',tenant:'film-demo',verified_account:'film-account',branding:{display_name:'B2B Support',primary_color:'#087f7d'}}));}
resetCustomerSession();
window.__filmApiLog=[];window.__missingFixtures=[];
window.fetch=async(input,init)=>{const u=new URL(typeof input==='string'?input:input.url,location.href);if(!API_PREFIXES.some(p=>u.pathname.startsWith(p)))throw Error('Non-fixture network blocked: '+u.pathname);window.__filmApiLog.push(u.pathname);const h=API_FIXTURES[u.pathname];if(h===undefined)window.__missingFixtures.push(u.pathname);const body=h===undefined?{items:[],total:0}:typeof h==='function'?h(u,init):h;return new Response(JSON.stringify(body),{status:200,headers:{'Content-Type':'application/json'}})};
const intervals=new Map();let timer=1;window.setInterval=(fn)=>{const id=timer++;intervals.set(id,fn);return id};window.clearInterval=id=>intervals.delete(id);window.__filmTick=()=>{for(const fn of [...intervals.values()])fn()};
class Silent{close(){}send(){}addEventListener(){}removeEventListener(){}}window.EventSource=Silent;window.WebSocket=Silent;
