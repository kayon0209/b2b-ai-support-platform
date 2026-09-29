export const NOW=1790647200;
export const state={sent:false,answer:false,claimed:false};
export const branding={display_name:'B2B Support',logo_url:null,primary_color:'#087f7d',support_email:null};
export const order={kind:'order_status',title:'SO-DEMO-2026',status:'in_production',status_label:'生产中',provenance:'demo',fetched_at:NOW,quantity:200,nodes:[{label:'订单确认',state:'done'},{label:'生产中',state:'active'}]};
const sourceTurns=[{turn_id:'film-turn-1',role:'customer',text:'请帮我确认订单 SO-DEMO-2026 的进度。',at:NOW-60,source:'web'},{turn_id:'film-turn-2',role:'agent',text:'示例订单正在生产，交期请由人工复核。',at:NOW-30,source:'ai'},{turn_id:'film-turn-3',role:'tool',text:'',at:NOW-29,source:'demo',card:order}];
export const turns=[sourceTurns[0],sourceTurns[2],sourceTurns[1]];
const lease=()=>({owner:state.claimed?'human':'queue',owner_ref:state.claimed?'film-agent':null,mode:state.claimed?'human':'queued',version:state.claimed?2:1,updated_at:NOW});
const item=()=>({conversation_ref:'film-conversation',title:'订单进度确认 · 演示客户',preview:'希望由同事继续确认交付安排。',last_at:NOW-10,channel:'web',contact_ref:'演示客户',case:null,lease:lease()});
export const API_PREFIXES=['/api/'];
export const API_FIXTURES={
'/api/v1/support/sessions':()=>({token:'synthetic-fixture-not-a-credential',conversation_ref:'film-conversation',expires_at:NOW+86400,visitor_id:'film-visitor',branding,support_window:{open:true,opens_at_hour:9}}),
'/api/v1/support/timeline':()=>({items:state.answer?turns:state.sent?[turns[0]]:[],conversation:{owner:'ai',mode:'ai'},branding,support_window:{open:true,opens_at_hour:9},rating_eligible:false}),
'/api/v1/support/messages':()=>{state.sent=true;return {status:'accepted',conversation:{owner:'ai',mode:'ai'}}},
'/api/v1/workbench/conversations':()=>({items:[item()],counts:{queue:state.claimed?0:1,mine:state.claimed?1:0,waiting:0},total:1,limit:50,offset:0,actor_ref:'film-agent',agent_name:'演示坐席',agent_status:'available',sort_mode:'activity',sort_scope:'queue',emotion_advice_enabled:false}),
'/api/v1/workbench/conversations/film-conversation':()=>({conversation_ref:'film-conversation',timeline_revision:4,lease:lease(),case:null,channel:'web',contact_ref:'演示客户',account:{name:'演示企业',tier:'standard',contract_status:'active',attributes:{},missing:[],contacts:[]},turns:[...turns,{turn_id:'film-turn-4',role:'customer',text:'请转人工，协助确认下一步安排。',at:NOW-5,source:'web'}],older_before:null,ai_suggestion:{text:'客户希望确认订单进度及后续安排。请核对业务记录后答复。',sources:['演示会话记录']},emotion_advice:null,can_review_emotion_advice:false}),
'/api/v1/workbench/conversations/film-conversation/actions':()=>{state.claimed=true;return {lease:lease()}},
'/api/v1/agents':{items:[{agent_id:'film-agent',name:'演示坐席',status:'available'}]},
'/api/v1/canned-replies':{items:[]},
'/api/v1/workbench/conversations/film-conversation/tasks':{items:[],total:0},
'/api/v1/workbench/standard-flows':{items:[],instances_enabled:false,execution_requires_tool_gateway:true}
};
