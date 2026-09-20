# 优化依据 · 官方机制与项目适用范围

查阅日期：2026-09-19。只查官方技术资料以校验替代方案的约束；不是线上产品检查。当前项目锁定Python 3.12.12和PySide6 6.10.3，参见[构建契约](../../../build-environment.json)、[锁文件](../../../uv.lock#L2839)。在线Python页面当前展示3.12系列补丁文档，Qt通用页面当前为6.11系列；本研究只引用长期存在的基础语义，不据此要求升级依赖。

| 官方来源 | 已核对的机制 | 对本方案的影响 / 不可推导内容 |
| --- | --- | --- |
| [Python 3.12 subprocess](https://docs.python.org/3.12/library/subprocess.html#subprocess.Popen.wait) | 管道无人消费时wait可能阻塞；communicate负责收集与等待；Popen超时和run超时的清理语义不同 | 统一runner必须保证输出消费和终止后收割，不能在GUI线程阻塞wait，也不能无界收集整场下载输出到内存 |
| [Qt QThread](https://doc.qt.io/qt-6/qthread.html#requestInterruption) | 中断请求由工作代码协作响应；quit仅退出事件循环；强制terminate有危险 | 将terminate换成requestInterruption不会自动修复阻塞Event或readline；需要明确唤醒点与资源owner |
| [SQLite WAL](https://www.sqlite.org/wal.html) | WAL仍只有一个同时写入者；同步设置影响提交持久性与断电边界 | 多开writer不能直接解决写入拥塞；应先减少无用写、明确ack。SQLite提交不覆盖用户媒体文件 |
| [SQLite atomic commit](https://www.sqlite.org/atomiccommit.html) | 提交依赖文件系统/存储对同步操作的承诺 | Staging JSON journal不是SQLite WAL；不能把后者的事务保证移植到多文件发布 |
| [Windows Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects) | Job可管理关联进程，子进程关联受创建方式与breakaway设置影响 | 进程scope可以使用Job，但须验证分配失败/嵌套Job，不能用同名进程或监听端口冒充所有权 |
| [Windows FlushFileBuffers](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-flushfilebuffers) | 可将指定文件缓冲送往设备，频繁调用有成本 | 关键提交检查点可研究同步，但不为每条进度fsync；不能只凭一个API承诺所有磁盘/网络盘断电原子性 |

上述资料约束接口设计。本项目是否正确排空管道、Job是否覆盖所有子进程、SQLite实际耐久性、文件组崩溃行为，仍需本目录验收矩阵中的隔离测试。
