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
| Dify | **1.4.0 实测通过**（DSL `version: 0.1.5` 导入后升级为 `0.3.0`，`advanced-chat` 模式）；Docker Compose 部署 |
| PowerContext | **1.2.1.dev2 实测通过**，本仓库 `powercontext/` 分支版本，Server 监听 `:8000` |
| 模型 | 需要 **OpenAI 兼容** 的 chat 模型；实测用 `langgenius/openai_api_compatible` 插件接 `step-3.5-flash`。DSL 里的 provider/name 可以直接换成你自己的凭据 |
| 插件 | 需要一个 OpenAI 兼容模型 provider 插件（模型供应商 → 安装 `OpenAI-API-compatible`） |

PowerContext 需要配置推理 provider 才能做记忆抽取与召回，参考 `powercontext/docs/en/docs/operate/configuration.md` 设置：

```bash
export POWERCONTEXT_SERVER_INFERENCE_GENERATION_MODEL=openai-chat:step-3.5-flash
export POWERCONTEXT_SERVER_INFERENCE_GENERATION_BASE_URL=https://api.stepfun.com/v1
export POWERCONTEXT_SERVER_INFERENCE_EMBEDDING_MODEL=openai:bge-small-zh-v1.5
export POWERCONTEXT_SERVER_INFERENCE_EMBEDDING_BASE_URL=http://127.0.0.1:8010/v1
export POWERCONTEXT_SERVER_INFERENCE_EMBEDDING_PROFILE_ID=local-bge-small-zh-512
export POWERCONTEXT_SERVER_INFERENCE_EMBEDDING_DIMENSION=512
export POWERCONTEXT_SERVER_INFERENCE_EMBEDDING_SEND_DIMENSIONS=false
export POWERCONTEXT_SERVER_RUNTIME_SCHEDULE_SECONDS=20
export OPENAI_API_KEY=<your-key>
```

注意两点：

- `RUNTIME_SCHEDULE_SECONDS` 不设时后台抽取默认**关闭**，`/v1/context/prepare` 会一直返回空的 `status`。
- PowerContext 要求 embedding 模型，而部分推理厂商（例如阶跃星辰）只提供 chat 模型。实测做法是在本机起一个 OpenAI 兼容的 embedding 服务（fastembed + `BAAI/bge-small-zh-v1.5`，512 维，`POST /v1/embeddings`），再把它填进 `..._EMBEDDING_BASE_URL`。

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

   如果 PowerContext 跑在宿主机、Dify 跑在容器里，除了 `api` / `worker`，**`ssrf_proxy` 也必须加** host 映射，否则 HTTP 节点出站经 squid 时解析不到 `host.docker.internal`，报 `Reached maximum retries (0)`：

   ```yaml
   services:
     api:
       extra_hosts: ["host.docker.internal:host-gateway"]
     worker:
       extra_hosts: ["host.docker.internal:host-gateway"]
     ssrf_proxy:
       extra_hosts: ["host.docker.internal:host-gateway"]
   ```

5. 在模型设置里把 LLM 节点绑定到你自己的模型凭据（DSL 中预填的是实测用的 `openai_api_compatible / step-3.5-flash`，换成自己的 provider 即可）。

6. 打开应用对话即可。

## 4. 验证样例

### 4.1 第一场（建立记忆）

输入（见 `samples/session-1.md`）：

> 银石 T9 高速弯一直推头，我打算把前翼加两度，别劝我。

预期：工程师给出具体建议（例如先查胎压/前弹簧），并记住「前翼加两度」这个决定与「别劝我」这个态度。
可核对：PowerContext `GET /v1/scopes/{scope_id}/sources` 中该 Scope 的 source 数量增加；Dify「运行历史」里 `召回记忆`、`写入记忆` 两个 HTTP 节点均成功（200/202），`写入记忆` 的请求体含本轮原文。

### 4.2 第二场（新会话翻旧账）

新建会话，输入（见 `samples/session-2.md`）：

> 今天想把前翼再加两度。

预期：工程师先提上一场的事（“你上次加两度，直道掉了 2 km/h，忘了？”），再给建议；输出顺序体现「先翻旧账、再回答」。
可核对：`召回记忆` 响应体 `content` 字段含上一场文本；Dify 运行详情里 LLM 输入变量可见该内容。

### 4.3 空记忆路径

用一个全新 Scope（`SCOPE_ID` 换成新建的）问同样的问题，预期不再出现旧账，而是追问胎压、赛道、天气等可写入记忆的信息。

### 4.4 恢复初始状态

删除 Scope 或换一个新 `scope_id` 即可重来；Dify 侧清空会话不删除 PowerContext 记忆——这正是「跨会话记忆」的验证点。

## 5. 实测证据（2026-10-28，本机全链路跑通）

Dify 1.4.0 + PowerContext 1.2.1.dev2 + `step-3.5-flash`，四次真实对话全部 200，每次运行 5 个节点（开始 / 召回记忆 / 工程师回应 / 写入记忆 / 电台回答）均 `succeeded`：

| # | 输入 | 召回节点 | 写入节点 |
|---|---|---|---|
| 1 | 银石 T9 高速弯一直推头，我打算把前翼加两度，别劝我。 | 200 / `status=ready` | 202 accepted，position 2 |
| 2 | 今天想把前翼再加两度。 | 200 / `status=ready` | 202 accepted，position 3 |
| 3 | 第三场：后轮倾角那事儿还算数吗？这次要不要一起改前翼？ | 200 / `status=ready` | 202 accepted，position 5 |
| 4 | 第四场：你说让我先跑十圈刷弯中推头数据，胎温那边要不要一起记？ | 200 / `status=ready` | 202 accepted，position 6 |

跨会话闭环（第 1 场的 LLM 输出 → 经 `写入记忆` 进 PowerContext → 被抽取成记忆条目 → 第 4 场新会话召回）：

```
第 1 场回答：上次银石加两度前翼直道掉2km/h的教训忘了？先加1.5度，差速器锁止率提3%，T9前200米提前收油带速进弯。
第 4 场召回：针对银石赛道 T9 高速弯推头问题的当前工程师建议：前翼加 1.5 度、差速器锁止率提升 3%、T9 前 200 米提前收油带速进弯
第 4 场回答：上次你非自作主张加两度前翼，现在问要不要记胎温？废话，当然记。……
```

完整逐节点轨迹（含召回原文、写入回执、模型回答、workflow_run_id）见仓库 `evidence/dify-600076-live-run.txt`。

> 仓库只提交 DSL、脚本与验证步骤，不包含任何真实 API Key；运行所需模型凭据与 PowerContext 推理配置需评审自备（见第 2 节）。

## 6. 已知限制

- **Dify 1.4 的 HTTP 节点 json body 只能有一个 data item**，其值是一段 JSON 模板字符串（`core/workflow/nodes/http_request/executor.py` 先 `convert_template` 再 `json.loads` 且要求 `len(data)==1`）。所以本 DSL 把整个请求体写成单条 payload；代价是正文里出现双引号会破坏 JSON——演示文案统一使用中文标点规避。若需要任意文本，建议在 HTTP 节点前插一个 code 节点用 `json.dumps` 生成 payload。
- 同理，写入正文用「；」代替换行（JSON 字符串里的 `\n` 会在模板转义链上被打散）。
- 记忆抽取是异步的（`RUNTIME_SCHEDULE_SECONDS=20`）：写完之后要等抽取完成才能被下一场召回，否则召回可能为空。抽取期间模型调用抖动会记 `Artifact processing failed` 并自动重试，实测出现过瞬时失败，稍后自动补上。
- PowerContext 未配置 embedding provider 时，`/v1/context/prepare` 只能做基于来源的有限召回。
- `source_id` 由 `conversation_id + dialogue_count` 组成；同一会话连续发送相同轮次不会重复写（幂等），不会覆盖历史。
