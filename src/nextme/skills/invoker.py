"""SkillInvoker — build prompts from skill templates.

Template variables
------------------
``{user_input}``  — replaced with the user's raw input text.
``{context}``     — replaced with session context (may be empty string).
"""

from __future__ import annotations

from .loader import Skill


class SkillInvoker:
    """Build fully-rendered prompts from :class:`~nextme.skills.loader.Skill` templates."""

    def build_prompt(self, skill: Skill, user_input: str, context: str = "") -> str:
        """Substitute ``{user_input}`` and ``{context}`` in *skill.template*.

        Parameters
        ----------
        skill:
            The skill whose ``template`` is the basis for the prompt.
        user_input:
            The user's request text; replaces every ``{user_input}``
            occurrence in the template.
        context:
            Optional session/project context string; replaces every
            ``{context}`` occurrence.  Defaults to an empty string.

        Returns
        -------
        str
            The fully-rendered prompt string.
        """
        result = (
            skill.template
            .replace("{user_input}", user_input)
            .replace("{context}", context)
        )
        # Claude global skills lack a {user_input} placeholder — append the
        # user's request so the agent still knows what to do.
        if user_input and "{user_input}" not in skill.template:
            result = f"{result}\n\nUser request: {user_input}"
        return result
