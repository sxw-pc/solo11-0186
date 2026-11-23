# 塔机吊装作业采证台

吊物方案、塔机索具人员和现场起落钩三账联动，决定起吊放行、停工、悬吊处置及完工。所有数据均为虚构离线样例，金额无涉；载荷使用千克，长度使用米，风速使用米/秒。本项目不控制真实设备。

Python 3.11+ / SQLite，合成数据离线运行。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m liftpermit demo --db demo.sqlite
python -m liftpermit report --db demo.sqlite
python -m liftpermit audit --db demo.sqlite
python -m unittest discover -s tests -v
```

独立入口：plan-import、asset-import、work-import。每项导入格式为 `--db state.sqlite --file fixtures/<name>.json`。同源重复幂等，异内容复用事件 ID 则整批回滚。`audit` 在空库重放原始文件并比对业务状态。

## 三套业务账与证据

- `plan-import` 从吊物估重与独立复测选当前重量，按方案版本计算重量乘半径的力矩；吊点支撑线/多边形须覆盖重心。回转路径线段与有效禁入区矩形相交则必须重画，旧签认不能跟随新版。持久化吊物测量、吊点方案和禁区公告。
- `asset-import` 用当前半径、臂长查塔机额定曲线并施加动态载荷系数；每根索具按角度折减后的能力、吊点数及有效检验期单独核对。指挥员资质与连续班次须覆盖作业窗，同塔机重叠预留会冻结许可。持久化塔机检验、索具证书、人员排班和预留。
- `work-import` 对有校准凭据的双源连续风速作停工与复工判定。离地未落钩遇资源故障或停工时，按吊物重量、回转半径、禁区和可用期选择应急落点；没有安全落点会单独报告。挂钩、离地、落钩、独立监理签收须按时间顺序完成，且事件晚于当前方案许可和复工授权。持久化作业许可、风速观测、应急落点和起落钩事件。

三个入口可在缺少其它账时独立采录并给出待核状态。完整演示依次导入正常、失败、恢复各三份 fixture，观察复测超载、索具报废/故障、双源超风速如何改变现场结果，再由新版方案、新资源和有效复工令恢复。`decision_history` 保留每次导入后的旧裁定；`audit` 从留存的原始字节在空库重算并与当前业务表比较，同时比对不可变的厂家页字节、快照与已发布曲线摘要。`demo` 报告打印 `imported_module`，用于确认导入的是本仓源码。

## 离线厂家门户与额定曲线取证

`liftpermit/portal.py` 是一个确定性的离线 mock 厂家门户（无网络），由 `fixtures/vendor-portal.json` 驱动：`TC1-rating` 同时提供已封版的 **V1**（月度签章 ZIP 归档，`1600/1500/1400`）与当前 **V2**（有期限读租约，`1200/1300`），两版都有逐页摘要与签章。

`liftpermit/vendor.py` 的 `VendorCollector` 按清单发现页数与链接，保存每页**原字节**、ETag、修订号、抓取时间与签章，并支持乱序、重复投递、断点续传（ETag/304）、总页数变化、旧缓存页、扫描中换版与租约过期。页字节以 `(portal_id, doc_id, revision, page_no, sha256)` 不可变入账，新旧快照并存。只有**同一修订全页覆盖 + 逐页摘要/签章校验 + 适用配重半径与臂长覆盖**同时成立，快照才标为 `certified`；否则为 `pending`/`ambiguous`，只暴露已取到的原页与 `gaps`，绝不生成合并曲线，也不会写入 `asset_rating_curve`。出版前还会对已存页重新计算摘要，防止认证后改写。

两种取证协议从同一“混合抓取”起点出发结论不同：

- `verify_archive(db, portal, 'TC1-rating', 'V1')`：校验月度签章 ZIP 的全页清单与逐页摘要，证明历史 V1；它不能冒充当前 V2（对当前修订请求归档会得到 `archive_not_published`）。
- `VendorCollector(...).capture()`：在租约内取当前 V2 全页并核对摘要，得到 `1200/1300`；租约中途过期则保持 `pending`，不补写历史 V1。

资源账只信任**已认证且已发布**的曲线：`publish_snapshot` 把 `certified` 快照的 rows 与证据（snapshot 摘要、逐页摘要）写入 `asset_rating_curve`。资产事件的 `rating_provenance` 若声明 `observed`，其曲线必须与该快照逐行一致且已发布，否则资源账置为 `frozen`、工作账为 `pending`；`absent` 或未知来源一律拒绝。历史归档曲线只代表其自身修订的真实数据（V1 的 `1600/1500/1400`），可放行 V1 覆盖的作业（J2 用真实 V1 会 `finished`），但门户拒绝为当前修订出具归档，因此它无法冒充 V2；当前 V2 的 `1200/1300` 则使同一 J2 停在 `pending`。两条协议因此给出不同版本、缺页范围与 J2 结论。回归样例见 `tests/test_vendor_portal.py`，它同时复现旧的 `import_page` 混合输出 `[1600,1300]` 与 J2 的 `finished`/`pending` 对照。

源码和测试现约 3000 行；已对八项首版能力、十二项决定、三条跨账路径以及厂家门户取证链路逐一写业务测试，但这不保证未来每仓可稳定产出十条困难题。正常和失败样例均为合成数据，未与真实安全规程或实际设备联调。
