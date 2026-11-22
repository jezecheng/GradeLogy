# 部署文档入口

本目录下原有的一键部署说明已过期，不再作为受支持的部署步骤。当前维护的部署流程只有：

- [单机 Docker 部署（Windows / macOS）](../docs/deploy-standalone.md)
- [腾讯云部署与 GitHub Actions 自动发布](../docs/deploy-tencent.md)

这两份指南说明当前持久化数据、备份与恢复、生产入口和安全边界。升级或迁移前先备份，不要直接照搬旧文档中的初始化脚本、端口开放建议、密钥配置或数据目录操作。

`deploy/init-server.sh` 和 `deploy/nas-update.sh` 仍保留在仓库中用于历史兼容；它们不构成上述受支持指南的必经步骤。运行前请先核对脚本内容与自己的环境，不确定时优先按维护中的部署指南手动操作。
