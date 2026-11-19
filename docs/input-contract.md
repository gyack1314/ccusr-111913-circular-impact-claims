# 家电多次流转的替代减排证据与声明核发资料契约

以下为项目本地合成资料，不代表真实居民、机构、金额或政策。所有数据为 UTF-8 JSON Lines，每行是一个完整对象；金额采用整数分，null 表示未知或不适用，不能自动等同于零。时间戳含 +08:00 偏移，日期为当地业务日。known_at 或 reported_at 控制资料何时可得，业务发生时间不意味着当时已经获知。

## 业务含义

功能服务按设备在约定用途下的有效使用年计算，2000服务小时为一个服务年，不能把所有权转移直接换成一整年。给出的环境因子是被替代新设备制造负担的合成演练范围，并非现实系数；burdens表示已经发生的维修及运输负担，需在净收益中保留。已观察小时、剩余寿命预测和替代率分别解释，未实现服务只属于情景。共同披露允许各方引用同一份服务声明，但汇总时只计一次；独占认领的同一设备服务区间互斥。service_to为不包含的终点，申请对象是服务期间与用途，不拥有设备全部生命周期效益。没有明确确认对应的serial空值不能自动匹配。

## 原始记录

### devices

| 字段 | 含义 |
| --- | --- |
| id | 资料设备身份 |
| serial | 可确认实物序列号，未知为空 |
| kind | 设备类别 |
| region | 使用地区 |
| model_age_years | 接收时机龄 |
| confirmed_same_as | 已确认对应身份，非猜测相似 |

### movements

| 字段 | 含义 |
| --- | --- |
| id | 流转事实 |
| device | 设备 |
| kind | 接收维修销售退回或在用 |
| day | 实际发生日 |
| known_at | 资料到达时间 |
| service_hours | 有记录支持的使用小时 |
| holder | 持有主体 |

### factors

| 字段 | 含义 |
| --- | --- |
| id | 因子身份 |
| kind | 设备类别 |
| region | 适用地区 |
| unit | 单位 |
| low | 因子支持下界 |
| high | 因子支持上界 |
| correlation_group | 共享来源不确定性组 |
| valid_from | 适用起点 |
| valid_to | 适用终点不含 |
| known_at | 获知时间 |

### substitution

| 字段 | 含义 |
| --- | --- |
| id | 调查记录 |
| kind | 设备类别 |
| region | 使用地区 |
| would_buy_new | 是否有明确新购替代意向 |
| observed_service_months | 已观察使用月数 |
| observation_complete | 该段观察是否完整 |
| known_at | 到达时间 |
| fold | 独立核对范围 |

### burdens

| 字段 | 含义 |
| --- | --- |
| id | 负担身份 |
| device | 对应实物 |
| kind | 维修运输或使用负担 |
| kgco2e_low | 已发生负担下界 |
| kgco2e_high | 已发生负担上界 |
| occurred_at | 实际时点 |
| known_at | 获知时点 |

### applications

| 字段 | 含义 |
| --- | --- |
| id | 引用申请 |
| applicant | 申请主体 |
| device | 设备 |
| service_from | 拟引用服务起日 |
| service_to | 拟引用服务止日 |
| mode | exclusive独占认领或joint共同披露 |
| known_at | 提交时点 |

