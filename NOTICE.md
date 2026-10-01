# NOTICE / 第三方组件说明

本仓库（globe-probe / gpm）原创代码以 Apache License 2.0 发布。

## 参考的开源项目（仅参考公开文档与行为语义，未复制代码）

| 项目 | 地址 | 许可证 | 参考内容 | 是否修改 |
| ---- | ---- | ------ | -------- | -------- |
| Uptime Kuma | https://github.com/louislam/uptime-kuma | MIT | 面板信息组织、拨测语义 | 否 |
| Komari | https://github.com/komari-monitor/komari | 以仓库 LICENSE 为准 | agent 拉模式/心跳架构思路 | 否 |
| 哪吒监控 Nezha | https://github.com/nezhahq/nezha | Apache-2.0 | 探针面板功能形态 | 否 |
| blackbox_exporter | https://github.com/prometheus/blackbox_exporter | Apache-2.0 | 探测参数语义（valid_status_codes 等） | 否 |
| mtr | https://github.com/traviscross/mtr | GPL-2.0 | **仅运行时子进程调用系统安装的 mtr 二进制**，解析其文本/JSON 输出格式 | 否（未复制源码） |
| ECharts | https://github.com/apache/echarts | Apache-2.0 | WebUI 图表库（本地分发 echarts.min.js，未修改） | 否 |

## 运行时外部命令

节点侧调用系统安装的 ping / curl / mtr 二进制（subprocess 数组参数，禁 shell）。
Apache-2.0 与"独立程序间的通信"边界：调用 GPL 工具的输出不构成本仓库的衍生作品。

## 数据隐私

探测目标与结果不出自建环境；不上传任何第三方服务；日志不记录 Token。
