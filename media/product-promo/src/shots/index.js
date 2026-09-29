import {Intro,Customer,WorkbenchShot,ApprovalShot,Close,buildScene} from './scenes.jsx';
export const SHOT_VIEWS={intro:Intro,customer:Customer,workbench:WorkbenchShot,approval:ApprovalShot,close:Close};
export const SHOT_BUILDERS=Object.fromEntries(Object.keys(SHOT_VIEWS).map(id=>[id,tl=>buildScene(tl,id)]));
