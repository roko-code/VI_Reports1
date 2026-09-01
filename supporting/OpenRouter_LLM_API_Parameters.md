# OpenRouter LLM API Parameters

| **Parameter** | **Purpose** | **Typical / allowable range** | **Example** |
| --- | --- | --- | --- |
| temperature | Controls randomness and creativity. Lower is more deterministic; higher is more varied. | Usually 0.0 to 2.0 | 0.2 for factual work, 0.8 for creative drafting |
| top\_p | Nucleus sampling; limits sampling to the most probable tokens until cumulative probability reaches the threshold. | Usually 0.0 to 1.0 | 0.9 for balanced output |
| max\_tokens | Maximum number of tokens the model may generate. | Positive integer; practical limit depends on model/context window | 300 |
| stop | One or more strings that stop generation when encountered. | Up to several strings, depending on model/provider support | \["\\n\\nEND"\] |
| seed | Attempts to make outputs more repeatable. | Integer; exact supported range is provider/model-dependent | 42 |
| response\_format | Requests structured output, often JSON. | Commonly {"type": "json\_object"}; schema support varies by model | {"type": "json\_object"} |
| tools | Declares functions/tools the model may call. | List of tool definitions | A lookup\_price function |
| tool\_choice | Controls whether tools may be used, must be used, or a specific tool must be called. | Common values: auto, none, required, or a specific tool choice object | "auto" |

**Practical notes**

-   temperature and top\_p both influence sampling, so you usually tune one first and leave the other near default.
-   max\_tokens is a cap, not a target; the model may stop earlier if it finishes.
-   stop is useful when you want the model to end at a delimiter.
-   seed can improve consistency, but you should not assume perfect determinism across all providers.
-   response\_format, tools, and tool\_choice matter most when you want machine-readable output or agent-style behavior.

**Python request template** (example code)

*python*

***from*** *openai* ***import*** *OpenAI*

*client = OpenAI(*

*base\_url="https://openrouter.ai/api/v1",*

*api\_key="YOUR\_OPENROUTER\_API\_KEY",*

*)*

*response = client.chat.completions.create(*

*model="openai/gpt-4o",*

*messages=\[*

*{*

*"role": "system",*

*"content": "You are a careful assistant that answers clearly and concisely."*

*},*

*{*

*"role": "user",*

*"content": "Return a JSON object with three reasons to use OpenRouter."*

*}*

*\],*

*temperature=0.2,*

*top\_p=0.9,*

*max\_tokens=300,*

*stop=\["\\n\\nEND"\],*

*seed=42,*

*response\_format={"type": "json\_object"},*

*tools=\[*

*{*

*"type": "function",*

*"function": {*

*"name": "lookup\_price",*

*"description": "Look up a product price by SKU.",*

*"parameters": {*

*"type": "object",*

*"properties": {*

*"sku": {"type": "string", "description": "Product SKU"}*

*},*

*"required": \["sku"\],*

*"additionalProperties": False*

*}*

*}*

*}*

*\],*

*tool\_choice="auto",*

*)*

***print****(response.choices\[0\].message.content)*

**Example settings by use case**

| **Use case** | **Suggested settings** |
| --- | --- |
| Factual answer | temperature=0.0–0.3, top\_p=1.0, moderate max\_tokens |
| Brainstorming | temperature=0.7–1.0, top\_p=0.9 |
| JSON extraction | temperature=0.0, response\_format={"type":"json\_object"} |
| Tool-using agent | temperature=0.1–0.4, tools=\[...\], tool\_choice="auto" |