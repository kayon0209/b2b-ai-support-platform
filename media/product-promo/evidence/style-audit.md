# 产品设计审计
来源：apps/admin-web/src/styles.css、styles-workbench.css、styles-support.css。
产品：白底、深蓝文字 #182638、青绿 #087f7d、线条 #dce5ed；工作台侧栏 #192b3e。主界面14px，8–12px圆角，少量阴影，细边线，中文系统无衬线。
影片：保留完整原样式，新增外层深蓝舞台/青绿会话线。没有正式独立 app icon，使用现有产品文字名称 B2B Support，不编造 Logo。
中文使用本机 PingFang SC，英文 Avenir Next；字体文件不重新分发。电影化包装自行实现，不使用 fallback CodePilot 组件。
主体 Workbench/SupportChat 均直接导入。ProposalPanel 通过构建器追加 export 暴露原始业务子组件；所有修改仅发生于视频构建内存，不改产品代码。
