import path from 'node:path';
import fs from 'node:fs/promises';
const repo=path.resolve('../..');
export default {repoDir:repo,aliases:{'@product':path.join(repo,'apps/admin-web/src')},define:{'import.meta.env':'{"DEV":true,"PROD":false,"VITE_API_BASE_URL":"/api"}'},esbuild:{plugins:[{name:'expose-original-proposal-panel',setup(build){build.onLoad({filter:/pages\/Approvals\.tsx$/},async args=>({contents:(await fs.readFile(args.path,'utf8'))+'\nexport { ProposalPanel };',loader:'tsx',resolveDir:path.dirname(args.path)}));}}]}};
