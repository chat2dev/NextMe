[用户记忆] (共 {{ count }} 条，可在回复末尾用 <memory> 标签更新)
{% for fact in facts %}{{ loop.index0 }}. {{ fact.text }}
{% endfor %}
记忆操作（仅在有必要时使用）：
- 新增: <memory>内容</memory>
- 更新: <memory op="replace" idx="0">新内容</memory>
- 删除: <memory op="forget" idx="1"></memory>
注意：<memory> 标签内容不会展示给用户，仅用于记录简短事实（< 500 字）。
