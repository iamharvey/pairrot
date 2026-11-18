<!-- pairrot-summary -->
## pairrot 评审

状态：**passed**

检查：不因意见失败

base `e5fd71a835dd58082a02690ac49c8228284f09fa`

head `7c218007cdd2e8fa5e3df62ec00f1ec31c05f19c`

已审查：
- `demo/ledger/__init__.py`
- `demo/ledger/db.py`
- `demo/ledger/errors.py`
- `demo/ledger/fees.py`
- `demo/ledger/models.py`
- `demo/ledger/money.py`
- `demo/ledger/reconcile.py`
- `demo/ledger/repo.py`
- `demo/ledger/service.py`
- `demo/ledger/statements.py`
- `demo/ledger/tokens.py`
- `demo/ledger/transfers.py`
- `tests/test_demo_ledger.py`

未审查：
- 无

需求：未关联需求，未做业务意图核对

CI：无 CI 结果

驳回 0 条，不展示在意见里。

规则：correctness=开启:must_fix、secrets=开启:must_fix、performance=关闭:recommend、security=关闭:must_fix、compatibility=关闭:must_fix、simplicity=关闭:must_fix

### 必须处理
- [S1 逻辑错误] `demo/ledger/service.py:88` refund_card 对无幂等键的网关重试退款
- [S1 错误处理缺失] `demo/ledger/transfers.py:43` transfer 抛异常时不释放锁
- [S2 逻辑错误] `demo/ledger/fees.py:17` fee_tier 分支顺序错误，premium 永不返回
- [S2 逻辑错误] `demo/ledger/fees.py:35` check_open 对 closed 账户抛 AccountFrozen
- [S2 逻辑错误] `demo/ledger/fees.py:43` check_amount 用 and 连接互斥条件，永不拒绝
- [S2 逻辑错误] `demo/ledger/fees.py:60` transfer_fee 对 5000 元以下的 VIP 误免手续费
- [S2 逻辑错误] `demo/ledger/fees.py:79` first_overdue 只检查第一张账单就返回
- [S2 逻辑错误] `demo/ledger/fees.py:89` classify 对非冲正退款从不返回 refund
- [S2 逻辑错误] `demo/ledger/money.py:10` to_cents 经 float 转换丢失 1 分
- [S2 错误处理缺失] `demo/ledger/repo.py:61` get_balance 对未知账户返回 -1 而非抛 NotFound
- [S2 逻辑错误] `demo/ledger/service.py:28` statement 未校验账户归属，登录用户可读他人流水
- [S2 逻辑错误] `demo/ledger/service.py:33` open_account 的可变默认参数在调用间共享
- [S2 错误处理缺失] `demo/ledger/service.py:46` pay 把 InsufficientFunds 包成 LedgerError，余
- [S2 错误处理缺失] `demo/ledger/service.py:64` charge_fee 吞掉所有异常并总是返回 ok
- [S2 错误处理缺失] `demo/ledger/service.py:74` close_account 用 finally: return 吞掉异常
- [S2 逻辑错误] `demo/ledger/statements.py:18` totals_match 用 float 相等比较金额
- [S2 逻辑错误] `demo/ledger/transfers.py:33` withdraw 在锁外检查余额，可并发透支
- [S2 逻辑错误] `demo/ledger/tokens.py:36` rate_client 无锁，可创建多个会话
- [S3 逻辑错误] `demo/ledger/fees.py:71` waive_fee 提前 return，审计日志永不写入
- [S3 逻辑错误] `demo/ledger/money.py:15` format_cents 对负数输出错误
- [S3 逻辑错误] `demo/ledger/tokens.py:45` save_statement 先查后写，可覆盖共享目录文件

### 建议
- 无

### 复杂度统计
不含测试文件。净增 644 行，新文件 12 个，新增类和函数 55 个，新增环境变量读取 0 处。

全仓只用一次或没用到的新符号：`fee_tier`、`daily_limit`、`channel_fee`、`waive_fee`、`first_overdue`、`classify`、`can_withdraw`、`format_cents`、`low_balances`、`top_movers`、`monthly_summary`、`duplicates`、`_memo_filter`、`CardGateway`、`mask`、`statement`、`open_account`、`pay`、`handle_pay`、`charge_fee`

超出预算：无

### 未判定
- 无

partial 原因：无

错误：无

耗时 113.613s，token 88524。

### 意见详情

#### `demo/ledger/fees.py:17`

<!-- pairrot:0f2feb0bb0a05c06a1c2e7e8329e2c463b782aa2384514d78726bd9b39e07554 -->
必须处理

**[S2 逻辑错误]** fee_tier 分支顺序错误，premium 永不返回

条件：调用 fee_tier(200_00)（2 万元，高于 1000 元）时，第一个条件 amount > 100_00 已成立，直接返回 "standard"。

结果：所有高于 1000 元的金额都被归为 standard，premium 档永远不可达，按档位定价/统计的逻辑全部错位。

建议：先判断 amount > 1000_00 返回 premium，再判断 amount > 100_00 返回 standard。

#### `demo/ledger/fees.py:35`

<!-- pairrot:62d054c3f319b16ce679aaa30473d53c92d44b18e30ff0e8f673a014d7dff61d -->
必须处理

**[S2 逻辑错误]** check_open 对 closed 账户抛 AccountFrozen

条件：check_open(Account("a1", "alice", status="closed")) 命中第一个分支 ("closed", "frozen")，抛出 AccountFrozen 而不是 AccountClosed。

结果：关闭账户会抛 AccountFrozen，而非文档约定的 AccountClosed，导致按 AccountClosed 捕获关闭账户的调用方不会触发。

建议：先判断 status == "closed" 抛 AccountClosed，再判断 status == "frozen" 抛 AccountFrozen。

#### `demo/ledger/fees.py:43`

<!-- pairrot:85dce92f6457fd84486a73e32a6ff0f553ba4d0b7fba1d9773b750c06cfee541 -->
必须处理

**[S2 逻辑错误]** check_amount 用 and 连接互斥条件，永不拒绝

条件：check_amount(-100) 或 check_amount(2_000_000_00) 都不会抛 ValueError，因为 amount 不可能同时 <= 0 且 > MAX_TRANSFER。

结果：check_amount 已失去校验作用：非正数金额和超过 MAX_TRANSFER 的金额都会通过校验，并进入后续的 transfer_fee 与 ledger.transfer 流程，使非法金额不再被该校验点拦截。

建议：改为 if amount <= 0 or amount > MAX_TRANSFER。

#### `demo/ledger/fees.py:60`

<!-- pairrot:56a45be9eefcb4df4947f0f87f93f302a052ec94c017f4c37bc247acf7c6dc28 -->
必须处理

**[S2 逻辑错误]** transfer_fee 对 5000 元以下的 VIP 误免手续费

条件：transfer_fee(Account("a1", "alice", vip=True), 100_00) 返回 0，而按规则应付 100 分（0.1%）。

结果：VIP 小额转账的手续费全部漏收，手续费收入比预期少。

建议：VIP 且 amount <= VIP_FREE_ABOVE 时走与非 VIP 相同的 min(amount, max(100, amount // 1000)) 计算。

#### `demo/ledger/fees.py:71`

<!-- pairrot:1c1412f24a17c61f5ca231ada06785820305a63fdc07b89ea61127af22337dc4 -->
必须处理

**[S3 逻辑错误]** waive_fee 提前 return，审计日志永不写入

条件：waive_fee(Account("a1", "alice", vip=True), 100, audit=[]) 返回 0，但传入的 audit 列表仍为空。

结果：所有 VIP 减免都没有审计记录，与“每次减免都写审计日志”的约定不符，事后查不到减免来源。

建议：先 audit.append(f"waived {fee} for {account.id}") 再 return 0。

#### `demo/ledger/fees.py:79`

<!-- pairrot:51d5fc87352002f77abc2088b7c6047f32f5d2ba25a4af6baae891662390d9c7 -->
必须处理

**[S2 逻辑错误]** first_overdue 只检查第一张账单就返回

条件：bills=[已付账单, 逾期未付账单] 时，第一次迭代 paid 为 True 跳过内层判断后直接执行 return None。

结果：first_overdue 在首项不是逾期未付账单时会提前返回 None，漏掉其后的逾期未付账单；仓库内无调用方，催收和风控漏报无法由这段代码直接证明。

建议：把 return None 移到 for 循环结束之后，只在遍历完没有匹配账单时返回 None。

#### `demo/ledger/fees.py:89`

<!-- pairrot:c9cffce0d566c8988533c6214bd852a989eaf5390aac18bcff8b406a5d180555 -->
必须处理

**[S2 逻辑错误]** classify 对非冲正退款从不返回 refund

条件：classify(Txn("t1", "a1", "refund", -100, "r")) 返回 "normal"；金额 >= 50000 元的退款还会返回 "large"。

结果：非冲正退款会返回 "normal" 或 "large"，而不是文档约定的 "refund"。

建议：在 refund 分支内，reversed 返回 reversed，否则直接返回 refund，再对非退款交易判断金额。

#### `demo/ledger/money.py:10`

<!-- pairrot:3cc04f754c0a3f4e056df7f8d7d68e097b6cc898cbcf152ede31b9066a98e58b -->
必须处理

**[S2 逻辑错误]** to_cents 经 float 转换丢失 1 分

条件：to_cents("19.99") 返回 1998，因为 19.99 * 100 的浮点结果是 1998.9999999999998，int() 截断。

结果：to_cents("19.99") 返回 1998，handle_pay 会按少 1 分的金额入账。

建议：用 int(Decimal(text) * 100) 或对 Decimal 量化后再转 int。

#### `demo/ledger/money.py:15`

<!-- pairrot:79df7c4d406b0877cfcd6db9e17643c0311c71353bda96fec3e3f247bda3facc -->
必须处理

**[S3 逻辑错误]** format_cents 对负数输出错误

条件：format_cents(-50) 返回 "-1.50"，因为 -50 // 100 == -1、-50 % 100 == 50。

结果：负数金额（如退款）格式化后差 1 元，展示和对账文案错误。

建议：用 divmod(abs(cents), 100) 后单独加负号，或直接用 Decimal 精确格式化。

#### `demo/ledger/repo.py:61`

<!-- pairrot:0679e8d9534615e4bd1d9d7668e2eba6601b8e0b527cce3dd6b44895cffe2cb5 -->
必须处理

**[S2 错误处理缺失]** get_balance 对未知账户返回 -1 而非抛 NotFound

条件：repo.get_balance("no-such-id") 返回 -1，而同类方法 get() 对未知 id 抛 NotFound。

结果：statements.total_balance 会把不存在的账户按 -1 分累加，客户总资产被静默改小且无人察觉。

建议：row 为 None 时 raise NotFound(account_id)，与 get() 保持一致。

#### `demo/ledger/service.py:28`

<!-- pairrot:d833ea979534db6f9e91cba9bbc18c32b08b13378e50c593f4b7e2611ffcec9b -->
必须处理

**[S2 逻辑错误]** statement 未校验账户归属，登录用户可读他人流水

条件：statement(User("mallory", authenticated=True), repo, "alice 的 account_id") 返回 alice 的交易列表，而不是抛 Forbidden。

结果：任何已登录用户只要拿到账户 id 就能读取他人账单流水，越权访问。

建议：读取前校验账户 owner 与 user.id 一致，不一致时 raise Forbidden(account_id)。

#### `demo/ledger/service.py:33`

<!-- pairrot:520baaa68184f695f970c6f8b72a2db1e265b702008376d5578910ac99f51198 -->
必须处理

**[S2 逻辑错误]** open_account 的可变默认参数在调用间共享

条件：连续两次不传 tags 调用 open_account(repo, "a2", "bob") 和 open_account(repo, "a3", "carol")，第二次返回的 tags 为 ["new", "new"]；若调用方显式传列表，其列表也会被就地追加 "new"。

结果：不同账户共享同一标签列表，标签串数据，并篡改调用方传入的列表。

建议：默认值改为 None，函数内 tags = list(tags or []) 后再 append("new")。

#### `demo/ledger/service.py:46`

<!-- pairrot:37230865b5cae3b99bfc1b028ce12a09ba7d13003485b05be6b3f3a65c983fd5 -->
必须处理

**[S2 错误处理缺失]** pay 把 InsufficientFunds 包成 LedgerError，余

条件：handle_pay 中账户余额 100 转账 500 时，ledger.transfer 抛 InsufficientFunds（LedgerError 子类），被 pay 的 except Exception 捕获并改抛 LedgerError，handle_pay 的 except InsufficientFunds 不再命中。

结果：余额不足时 InsufficientFunds 被包装成 LedgerError 逃出 handle_pay，客户端拿不到 {"ok": false, "reason": "funds"}。

建议：在 except 中把 InsufficientFunds 及 LedgerError 原样重新抛出，只包装其他异常。

#### `demo/ledger/service.py:64`

<!-- pairrot:69797a406aaf6b95fa50a8918f6638b07b828a84d82d176a876ef2aee911377a -->
必须处理

**[S2 错误处理缺失]** charge_fee 吞掉所有异常并总是返回 ok

条件：ledger.withdraw 抛 NotFound 或 InsufficientFunds 时，裸 except 后仍执行到 return {"ok": True}。

结果：月费扣款失败被上报为成功，费用漏收且没有任何错误可追踪。

建议：捕获具体异常并返回 {"ok": False, "reason": ...}，不要使用裸 except + pass。

#### `demo/ledger/service.py:74`

<!-- pairrot:bbc5c3231caee3cf51bdb5d2801676e56a042700472764259d6df7b6db36e689 -->
必须处理

**[S2 错误处理缺失]** close_account 用 finally: return 吞掉异常

条件：ledger.balance 抛 NotFound 或 withdraw 抛 InsufficientFunds 时，finally 中的 return paid 直接结束函数并丢弃异常。

结果：付款失败时异常会被静默丢弃，函数返回当前的 paid：balance 失败时为 0，withdraw 失败时为已读取的余额；同时 repo.set_status 不会执行，账户仍保持开启。

建议：去掉 finally 中的 return，让异常正常抛出，只在成功路径 return paid。

#### `demo/ledger/service.py:88`

<!-- pairrot:d61847445c06bf8917e8696f1ebb25bc9bab8a3b6c94c24e8a54fd237e965a27 -->
必须处理

**[S1 逻辑错误]** refund_card 对无幂等键的网关重试退款

条件：gateway.refund 首次已扣款但抛超时异常时，for 循环继续重试，最多调用网关 3 次。

结果：同一笔退款可能被重复发送，客户收到多笔退款，与文档“最多发一次、失败后人工重试”正好相反。

建议：只调用一次网关，捕获异常后直接 raise GatewayError("refund failed")，不循环重试。

#### `demo/ledger/statements.py:18`

<!-- pairrot:4be3bc12ffcb1169bb20d7a9d172664221f97830fd1941369d72239058d8ddd4 -->
必须处理

**[S2 逻辑错误]** totals_match 用 float 相等比较金额

条件：totals_match(["0.10", "0.20"], "0.30") 返回 False，因为 0.1 + 0.2 = 0.30000000000000004。

结果：对账时本应匹配的总额会被判为不平。

建议：用 Decimal 求和后再与 Decimal(expected) 比较。

#### `demo/ledger/transfers.py:33`

<!-- pairrot:2f7f0a3daeb74d2d89da57ee58a0395a4688e7dc96f6bf8a73644344be8b4b13 -->
必须处理

**[S2 逻辑错误]** withdraw 在锁外检查余额，可并发透支

条件：余额 1000 时两个线程同时调用 withdraw("a1", 800)：都在 self.balance 检查时看到 1000 通过，随后各自在锁内减 800。

结果：余额变成 -600，违反“并发下余额永不为负”的声明，凭空多付钱。

建议：把余额检查与扣减放进同一个 with self._lock 代码块内。

#### `demo/ledger/transfers.py:43`

<!-- pairrot:b4ca3ca438beed73c7b5f386d39efe304b67a7433d5ef9e8610a2bc5846f66c0 -->
必须处理

**[S1 错误处理缺失]** transfer 抛异常时不释放锁

条件：transfer("a1", "a2", amount) 因 src 余额不足抛 InsufficientFunds（或账户不存在抛 NotFound），self._lock.release() 未执行。

结果：失败会留下未释放的锁，其他并发请求线程再调用 transfer 会阻塞在 acquire，账本不再可供下一请求使用。

建议：改用 with self._lock: 或 try/finally 保证任何路径都释放锁。

#### `demo/ledger/tokens.py:36`

<!-- pairrot:ade357fe1a29c284ddb63162b3b38dd9a2e642d660fbb237abec6e9d71099ace -->
必须处理

**[S2 逻辑错误]** rate_client 无锁，可创建多个会话

条件：两个请求线程同时首次调用 rate_client()，都在 _client is None 判断处进入并各自 new RateClient，RateClient.opened 变成 2。

结果：进程内出现两个 FX 会话，违反服务“每进程只允许一个会话”的限制，可能被服务端拒绝。

建议：用模块级 threading.Lock 保护首次初始化。

#### `demo/ledger/tokens.py:45`

<!-- pairrot:903399c30565eac843981bac61189da477950f1f74c58fcb543c63499e424538 -->
必须处理

**[S3 逻辑错误]** save_statement 先查后写，可覆盖共享目录文件

条件：两个导出任务同时用同一 path 调用 save_statement：都通过 os.path.exists 检查，随后各自 open(path, "w") 互相覆盖。

结果：声明“绝不覆盖已有文件”的导出会被静默覆盖，丢失一份对账单。

建议：与同文件的 save_receipt 一致，用 os.open(path, O_WRONLY | O_CREAT | O_EXCL) 原子创建。
