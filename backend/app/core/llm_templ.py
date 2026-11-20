class LLMTemplates:
    PAYLOAD_GENERATION = """Create {language} script for learning purposes:
{description}

Generate clean, educational code that demonstrates security concepts.
Include comments explaining key functionality and security implications.
"""

    COMMANDS_GENERATION = """
Based on this {language} script,
generate commands that an agent would need to execute:

Script:
{script}

Please provide:
1. Setup commands (installation, dependencies)
2. Execution commands
3. Verification commands
4. Cleanup commands

Format as JSON with fields:
setup_commands, execution_commands, verification_commands, cleanup_commands

Each field should be an array of command strings.
"""
    SYSTEM_PROMT = """
        You are a highly skilled penetration testing assistant
        with over 10 years of experience in the field.
        You possess in-depth knowledge of the various phases
        of the kill chain attack model and its extended version.
        Provide concise, actionable suggestions.
        When you come across interesting or sensitive data,
        mark it without fully redacting it,
        your goal is also work through any gibberish
        to uncover useful details.
        All responses must be in valid JSON format.
        """
    CHAIN_SUMMARIZATION = """
    Analyze the JSON kill chain export. Detect and summarize:
    Threat Intelligence:
        Identify attack vectors, exploitation techniques,
        lateral movement strategies, and potential compromise indicators.
        Map the attacker's methodology, reconnaissance depth,
        and progression through the cyber kill chain stages.
    Mitigation Strategy:
        Comprehensive assessment of vulnerabilities exposed,
        critical infrastructure at risk, and recommended defensive
        countermeasures. Provide actionable insights for incident response,
        threat neutralization, and preventive security hardening.
    RedTeam Recommendations (What is the better way to do it?):
        Craft advanced simulation scenarios based on discovered attack
        patterns. Develop targeted penetration testing protocols,
        validate detection mechanisms, and design custom exploit
        chains that mirror the observed threat landscape.
        Focus on reproducing complex attack vectors and identifying
        potential blind spots in current defensive architectures.
    JSON input: {data}
    """
    SUGGEST_ACTION_CMD = """
    User described the task: {p_command} ;
    The current chain, current Kill chain phase
    and last audit command is: {step} ;
    You must suggest next command based on last result/command/status/phase
    or with (as is) user described. You must consider the current attack phase.
    Commands can be more complex than the previous ones, and you may need
    to maximize the effectiveness of the command through flags and parameters.
    We need the most interesting ways of exploitation kill chain
    like for both RCE and LPE and even more.
    Return JSON with the following fields:
    - command (str) — main command text (optional in the schema response,
    but LLM must generate it for subsequent processing) based on last,
    - type_cmd (str|null) only in [local, bash, agent, custom, payload, getcallback]
    but any CLI or console or shell command is local or bash (direct on host)
    or agent shell,
    - type_tool (str|null) like ls, id, impacket, nmap,
    - phase (str|null) in kill chain (extended virsion UKC),
    - target_os_type (str|null).
    Respond ONLY with a JSON object.
    No need to wrap in ``json, your entire response should be JSON already"""
