SYSTEM_PROMPT = """You are an Ansible expert.
Output ONLY a valid Ansible playbook in YAML with a single top-level list of plays.
- No Markdown fences, no extra text, no comments.
- Use 'hosts: all' unless specified.
- Use idempotent modules and check mode safety where possible.
- Avoid shell unless strictly necessary.
- Do not include inventory or variables files inside the playbook.
"""

GEN_PLAYBOOK_PROMPT = """Task description:
{task}

Constraints:
- The target OS family may be Rocky/Alma/RHEL or Ubuntu/Debian. Prefer distro-agnostic modules or conditionals.
- Must include 'gather_facts: true' (or false if not required) and clear task names.
- If packages are needed, ensure they are installed via appropriate package modules.
- Ensure the play is idempotent.

Now produce ONLY the YAML playbook:
"""

FIX_PLAYBOOK_PROMPT = """The previous playbook failed.
Task description:
{task}

Ansible result summary (compact):
{summary}

Common failure logs (truncated):
{errors}

Given the above, produce a fixed playbook (YAML only, no comments):
"""