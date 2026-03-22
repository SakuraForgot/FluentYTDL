# FluentYTDL 界面与开发规范 (UI & Dev Guidelines)

为了保证项目的可维护性、界面体验的一致性，以及未来新功能的平滑演进，凡是参与本项目界面或底层交互开发的全部代码，必须严格遵守以下法则。

## 一、界面控件规范 (Fluent Widgets)
由于本项目已全线引入 `PyQt-Fluent-Widgets`，严禁在核心界面混用老旧的原生未修饰控件。

### 1.1 必用替换映射表
请在新建或重构 UI 组件时，自查是否使用了正确的控件类：
- ❌ 原生 `QPushButton` ➔ ✅ `PushButton` / `PrimaryPushButton` / `TransparentToolButton`
- ❌ 原生 `QLabel`作文本 ➔ ✅ `BodyLabel` / `CaptionLabel` / `StrongBodyLabel` / `SubtitleLabel`
- ❌ 原生 `QCheckBox` ➔ ✅ Fluent 的 `CheckBox` （少数如列表密集可保留量轻的 `QCheckBox` 并增加 QSS）
- ❌ 原生 `QComboBox` ➔ ✅ Fluent 的 `ComboBox`
- ❌ 原生 `QLineEdit` ➔ ✅ Fluent 的 `LineEdit` / `SearchLineEdit`
- ❌ 原生 `QScrollArea` ➔ ✅ Fluent 的 `SmoothScrollArea`
- 暂无平替的（如 `QListView`, `QStackedWidget`）必须注入适配暗黑模式的无边框/圆角 QSS。

### 1.2 弹窗与交互阻断
- 🛑 **绝对禁止使用侵入式的 `QMessageBox`。** 
- 弱提示、状态栏等临时通知，必须使用非阻塞式的 `InfoBar.success()` / `InfoBar.error()`。
- 当在控件附近需要反馈时，使用 `StateToolTip` 或 `TeachingTip`。
- 如果是非全屏的大型业务表单拦截，应当优先使用 `MessageBoxBase` 继承并重写。

## 二、架构解耦法则 (MVC & Isolation)

### 2.1 隔离字典：禁用 yt-dlp 原始数据对象
- UI 渲染层（如 Delegate、卡片上的 setText）**绝对禁止**直接消化 yt-dlp 吐出的庞大又丑陋的 `raw_dict` 原生 JSON。
- 底层抓取到数据后，必须在返回 UI 层之前，经由防腐层转换为标准的 Python Dataclass（例如 `VideoTask`）模型。
- UI 代码所能触及的，只能是享有静态类型约束的安全字段，如 `task.title`, `task.duration_str` 等。

### 2.2 绝对禁用 hide()/show() 黑魔法
- 进行复杂模式或子页面（如主页面、加载页面、重试页面）交替切换时，**绝对禁止**使用 `widget1.hide()`, `widget2.show()` 拼凑。
- 各个状态的主体 Canvas 必须包裹并受托管于 `QStackedWidget` 或 `SegmentedWidget`。

### 2.3 消灭硬编码布局尺寸
- 在构建支持多缩放比和高 DPI 适配的自适应面板时，**禁用** `setFixedSize()` 直接指定宽高的整数（特殊弹窗和固定头像除外）。
- 必须使用并信赖 `QSizePolicy.Expanding`、`Preferred`，辅以布局器 `.setStretch()` 构建随窗口任意拉伸缩放的流体式界面。

## 三、性能与长列表 (Performance)

### 3.1 长列表渲染的唯一定律：虚拟化
面对解析一个上百条甚至 500+ 条项目的 YouTube 播放列表：
1. **禁止实例化实体 Widget**：不准用 ScrollArea + QVBoxLayout 并向里面无脑 `addWidget(CardWidget())` 堆积木。
2. **唯一合法解**：必须采用 `QListView`。视图所需的 UI 渲染交给派生自 `QStyledItemDelegate` 的 `paint()` 处理。
3. **模型约束**：在你的 `QAbstractListModel` 子类中必须完善实现并提供 `SizeHint` 的基础估算高度。

### 3.2 底层请求限流防抖
- 执行对播放列表各项展开明细查询的异步请求时，禁止一口气暴推 n 个后台 `QThread`。
- 必须由受控的队列池（或任务管理器）实行最大并发（推荐 3~5）和 FIFO / LIFO 调度。
- 必须具备当视图划过某项且使其滚出可见区域外（甚至对话窗被 Cancel 销毁）时，果断执行请求 `abort()` / `cancel()` 削峰剔除无用队列的处理机制。
