# 雨云游戏云管理 (astrbot_plugin_rainyun_rgs)

基于雨云开放 API（https://api.rainyun.com/）的 AstrBot 插件，用于查询和管理雨云游戏云（RGS）实例。

## 指令

| 指令 | 说明 |
| --- | --- |
| `/雨云列表` | 列出账号下全部游戏云实例（状态、配置、剩余天数） |
| `/雨云详情 [实例ID]` | 查看单个实例详情（区域、地址、到期时间、面板用户等） |
| `/雨云状态` | 快速查看默认实例的开关机状态 |
| `/雨云开机 [实例ID]` | 开机 |
| `/雨云关机 [实例ID]` | 关机 |
| `/雨云重启 [实例ID]` | 重启 |

不带实例 ID 时，优先使用插件配置里的「默认实例 ID」；若未配置且账号只有一个实例，会自动选中它。

## 配置项

- **api_key**：雨云 API 密钥，在「用户中心 → API 密钥」创建
- **base_url**：默认 `https://api.v2.rainyun.com`
- **admin_only**：仅管理员可用（默认开启，开关机强制要求管理员）
- **allow_power_ops**：是否允许开关机/重启（默认开启）
- **default_rgs_id**：默认实例 ID

## 用到的接口

- `GET /product/rgs/` 实例列表（`options` 传 JSON 分页参数）
- `GET /product/rgs/{id}/` 实例详情
- `POST /product/rgs/{id}/start` 开机
- `POST /product/rgs/{id}/stop` 关机
- `POST /product/rgs/{id}/reboot` 重启

鉴权方式：请求头 `X-Api-Key`。

## 注意

实例关机时，`/product/rgs/mcsm/status`、`/product/rgs/{id}/usage` 等依赖容器的接口会返回 404（找不到对象），属于正常现象。

## 积分自动续费

插件会定时检查实例到期时间，剩余天数 ≤ 阈值（默认 7 天）时自动用雨云积分续费：

1. 先尝试续 **30 天**（一个月）
2. 若失败（通常是积分不足），自动退回续 **7 天**
3. 两者都失败则记录失败原因

同一实例有失败冷却（默认 6 小时），避免反复请求接口。

相关指令：

| 指令 | 说明 |
| --- | --- |
| `/雨云续费` | 立即检查并续费一次（跳过阈值与冷却判断） |
| `/雨云自动续费` | 查看自动续费配置与上次检查结果 |

接口：`POST /product/rgs/{id}/renew/`，body 为 `{"duration_day": 30|7, "product_id": id, "product_type": "rgs"}`。
注意 `duration_day` 雨云只支持 **7** 或 **30** 两档。

新增配置项：`auto_renew_enabled`、`renew_threshold_days`、`renew_primary_days`、`renew_fallback_days`、`renew_interval_hours`、`renew_cooldown_hours`、`renew_instance_ids`、`allow_renew_ops`、`notify_umo`。
