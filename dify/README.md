# Pit Wall 电台 · 会记仇的赛车工程师（Dify × PowerContext）

一个「越聊越有梗」的角色互动应用：你是车手，对面是嘴很毒的赛车工程师。你每次找借口、改设定、许下承诺，都会被 PowerContext 记住；下一场比赛（甚至新会话）它会翻旧账，拿你上次说的话挤兑你。

- Dify 负责：对话入口、记忆召回、提示词编排、模型调用、结果输出。
- PowerContext 负责：保存每轮对话（`/v1/sources/content`）并按当前问题召回相关记忆（`/v1/context/prepare`）。
- 不使用 seekdb；本作品按题意只选 PowerContext 一种指定技术。

## 1. 目录

```
dify/
├── app.yml                 # Dify 应用 DSL（advanced-chat，4 个节点）
├── scripts/init_scope.sh   # 创建 PowerContext Scope 并写入开场记忆
├── samples/
│   ├── session-1.md        # 第一场：埋下「前翼加两度」的承诺
│   └── session-2.md        # 第二场：新会话里被翻旧账
└── README.md
```

DSL 节点顺序：`开始 → 召回记忆（HTTP）→ 工程师回应（LLM）→ 写入记忆（HTTP）→ 电台回答（Answer）`。

## 2. 环境

| 组件 | 版本 / 说明 |
|---|---|
| Dify | 1.x（DSL `version: 0.1.5`，`advanced-chat` 模式）；Docker Compose 部署 |
| PowerContext | 本仓库 `powercontext/` 主线版本，Server 默认监听 `127.0.0.1:8000` |
| 模型 | 任一 OpenAI 兼容 chat 模型（示例占位 `openai / gpt-4o-mini`，可换成自建网关） |
| 插件 | 不需要额外插件；只用 Dify 内置 HTTP Request 节点 |

PowerContext 需要配置推理 provider 才能做记忆抽取与召回，参考 `powercontext/docs/en/docs/operate/configuration.md` 设置：

```bash
export POWERCONTEXT_SERVER_INFERENCE_GENERATION_MODEL=openai:gpt-4o-mini
export POWERCONTEXT_SERVER_INFERENCE_EMBEDDING_MODEL=openai:text-embedding-3-small
export POWERCONTEXT_SERVER_INFERENCE_EMBEDDING_PROFILE_ID=openai-3-small-1536
export POWERCONTEXT_SERVER_INFERENCE_EMBEDDING_DIMENSION=1536
export OPENAI_API_KEY=<your-key>
```

未配置推理 provider 时，`/v1/sources/content` 仍能写成功，但记忆抽取与按语义召回不会生效。

## 3. 启动步骤

1. 启动 PowerContext（Docker）：

   ```bash
   docker build -f powercontext/docker/Dockerfile -t powercontext-server:local powercontext
   docker run -d --name powercontext-server -p 127.0.0.1:8000:8000 \
     -v powercontext-data:/data --env-file .env powercontext-server:local
   curl -sS http://127.0.0.1:8000/health/ready
   ```

2. 创建 Scope 并写入开场记忆：

   ```bash
   cd dify && chmod +x scripts/init_scope.sh
   POWERCONTEXT_URL=http://127.0.0.1:8000 ./scripts/init_scope.sh "Pit Wall 电台"
   ```

   记下输出的 `scope_id`。

3. 启动 Dify（官方 `docker/docker-compose.yaml`），在「工作室 → 导入 DSL」上传 `dify/app.yml`。

4. 打开应用 →「环境变量」：

   - `POWERCONTEXT_URL` 填 Dify 容器能访问到的地址。Docker 部署时用 `http://host.docker.internal:8000`（本 DSL 已预填）；PowerContext 与 Dify 同网络时用服务名。
   - `SCOPE_ID` 填第 2 步的 `scope_id`。

5. 在模型设置里把 LLM 节点绑定到你自己的模型凭据（DSL 中的 provider/name 是占位值）。

6. 打开应用对话即可。

## 4. 验证样例

### 4.1 第一场（建立记忆）

输入（见 `samples/session-1.md`）：

> 银石 T9 高速弯一直推头，我打算把前翼加两度，别劝我。

预期：工程师给出具体建议（例如先查胎压/前弹簧），并记住「前翼加两度」这个决定与「别劝我」这个态度。
可核对：PowerContext `GET /v1/scopes` 中该 Scope 的 source 数量增加；Dify「运行历史」里 `召回记忆`、`写入记忆` 两个 HTTP 节点均成功（200/202），`写入记忆` 的请求体含本轮原文。

### 4.2 第二场（新会话翻旧账）

新建会话，输入（见 `samples/session-2.md`）：

> 今天想把前翼再加两度。

预期：工程师先提上一场的事（“你上次加两度，直道掉了 2 km/h，忘了？”），再给建议；输出顺序体现「先翻旧账、再回答」。
可核对：`召回记忆` 响应体 `content` 字段含上一场文本；Dify 运行详情里 LLM 输入变量可见该内容。

### 4.3 空记忆路径

用一个全新 Scope（`SCOPE_ID` 换成新建的）问同样的问题，预期不再出现旧账，而是追问胎压、赛道、天气等可写入记忆的信息。

### 4.4 恢复初始状态

删除 Scope 或换一个新 `scope_id` 即可重来；Dify 侧清空会话不删除 PowerContext 记忆——这正是「跨会话记忆」的验证点。

## 5. 证据材料

- Dify「运行历史」→ 每次对话的节点执行记录（含两个 HTTP 节点的状态码、请求 JSON、响应 JSON）。
- PowerContext 侧：`GET /v1/scopes` 的 source 计数、`POST /v1/context/prepare` 的返回内容。
- 本仓库附 `samples/` 的操作脚本；按第 4 节顺序执行即可复现同样的对话。

> 说明：本目录提交的是可直接导入的 DSL、初始化脚本和验证步骤；运行所需模型凭据与 PowerContext 推理配置需评审自备（占位值见第 2 节），仓库不包含任何真实 Key。

## 6. 已知限制

- 记忆抽取是异步的：第一场对话后建议等待数十秒再开第二场，否则召回可能为空。
- PowerContext 未配置 embedding provider 时，`/v1/context/prepare` 只能做基于来源的有限召回。
- `source_id` 由 `conversation_id + dialogue_count` 组成；同一会话连续发送相同轮次不会重复写（幂等），不会覆盖历史。
