# STRC 持仓监控

按分红、BTC 覆盖倍数和管理层行为给 Strategy 的 STRC 优先股分级预警（安全 / 黄 / 橙 / 红）。不构成投资建议。

- `index.html`：监控网页，单个文件，不依赖外部库。
- `scripts/update.py`：后台脚本，只用 Python 标准库。
- `.github/workflows/update.yml`：GitHub Actions 定时任务，计划每 15 分钟运行一次脚本。GitHub 的定时任务不保证准时，繁忙时可能隔几个小时才跑一次；2026 年 11 月下旬实际是 4 到 10 小时一次。`scripts/` 或这个文件有改动、合并到 `main` 时也会马上跑一次。
- `data/latest.json`：脚本生成的数据，网页读取它。`data/state.json` 是脚本自己的缓存和提醒记录。

## 后台自动做的

每次运行时：

1. 从 SEC 读 Strategy 的文件列表，解析 2026-10-01 之后的每份 8-K 正文（周报在附件 99.1 里的也会读）。
2. 从周报 8-K 里取 BTC 持仓、美元储备和美元现金、优先股回购剩余额度、各优先股的 ATM 增发和回购股数，从而推算 STRC 等优先股的面值。
3. 从利率公告和分红宣告里取 STRC 当月利率，以及每只优先股已经宣告到哪一天。
4. 从 Yahoo 取 STRC 的收盘价，从 Coinbase 取 BTC 周线收盘和 USDT 价格。
5. 生成信号，写进 `data/latest.json`。有新信号时开一个带 `strc-signal` 标签的 Issue，GitHub 会给仓库的关注者发邮件或 App 推送。同一个信号只提醒一次。

Strategy 改了 8-K 的措辞、后台没读出新周报时，会开 Issue 提醒，网页顶部也会显示，并附上那份 8-K 的链接。2026-11-23 的 8-K 在余额那句话中间插了一句 “after giving effect to the reallocation”，旧的解析规则因此漏读了一周，之后改成先找余额、再往前找日期，日期也可以从 “During Period A to B” 里取。

自动触发的信号：

- 8-K Item 1.03（破产）、2.04（债务加速到期）、4.02（财报重述）。
- STRF、STRK、STRD 过了已宣告的期限还没有新宣告，以及 STRC 本身没按时宣告。
- STRC 收盘低于 100 美元时下调利率。
- 回购额度低于 0.5 亿美元满 30 天，且 STRC 收盘低于 95 美元。

需要你在网页上确认的：

- Item 2.03（新增债务，会附上正文里提到抵押的句子）、4.01、1.05、3.03、5.03，以及股东大会代理声明。
- 每股净得低于 98.5 美元的 STRC 增发。
- 美元储备出现付息以外的用途，或者储备政策有调整（比如 2026-11-23 把最多 10 亿美元从美元储备转到美元现金）。
- 美元现金一周内用在付息和回购优先股以外的地方（买 BTC、回购普通股、还可转债等）合计超过 0.5 亿美元。
- BTC 持仓比披露的卖出量减少得多。
- 可转债有回购、赎回、交换或转股的描述（只提示，请核对网页里的可转债金额）。

仍然要自己看新闻的：聘请重组顾问、Tether 暂停赎回、2027 年回售的偿还方案。

## 设置

1. 仓库设为公开（免费账号的 GitHub Pages 只支持公开仓库）。页面里没有你的持仓信息，你在网页里填的数据只存在你自己的浏览器里。
2. **Settings > Pages**：Source 选 “Deploy from a branch”，Branch 选 `main`，文件夹选 `/ (root)`。几分钟后网页地址是 `https://<你的用户名>.github.io/<仓库名>/`。
3. **Settings > Secrets and variables > Actions**：新建一个名为 `SEC_CONTACT` 的 secret，值填你的邮箱。SEC 要求自动程序留真实联系方式，GitHub 的 noreply 邮箱会被拒绝。没设置时脚本会开一个 Issue 提醒你。
4. **Actions** 页面：打开 “更新 STRC 监控数据”，点 “Run workflow” 先手动跑一次。
5. 想收到提醒：确认你在关注（Watch）这个仓库，并在 GitHub 的通知设置里打开邮件或手机 App 推送。

## 需要手动维护的常量

`scripts/update.py` 开头有几项从 2026 年 11 月的文件里取的基准数：

- `BASELINE_SHARES`：2026-11-08 各优先股的股数。之后的变化按每周 8-K 累加，时间长了可能有少量误差，可以按最新 10-Q 校正。
- `CONVERTS_YI`：可转债合计（亿美元），只用于后台推送覆盖倍数提醒。可转债有变动（回购、回售、新发）时要改这里，网页里的可转债表也要改。

改了解析规则时，把 `PARSER_VERSION` 加 1，旧的解析缓存会作废重算。

## 本地运行

```bash
SEC_CONTACT=你的邮箱 python3 scripts/update.py
```

没有 `GITHUB_TOKEN` 时不会开 Issue，只在终端打印要提醒的内容。直接用浏览器打开本地的 `index.html` 也能用，但读不到 `data/latest.json`，8-K 里的数字要手动填。
