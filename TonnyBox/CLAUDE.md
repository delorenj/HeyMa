# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**{{PROJECT_NAME}}** is a {{PROJECT_DESCRIPTION}}. This project is part of the {{PLANE_WORKSPACE}} ecosystem.

### Key Components
- **{{AGENT_NAME}}** - Primary agent for {{AGENT_PURPOSE}}
- **{{CORE_TECHNOLOGY}}** - Core stack and infrastructure

## Development Workflow

### BMAD Methodology
This project follows the BMAD (Business Method for Agile Development) methodology. Key requirements:

1. **Strict BMAD Adherence** - All prompts and tasks must follow BMAD patterns
2. **Component Delegation** - Work on components must be delegated to specialized agents
3. **Agent Creation** - All agents must be created using BMAD agent creation workflow
4. **Session Verification** - Begin each session with verbose simulation of intended actions

### Ticket Management (MANDATORY)

**No code changes without an active Plane ticket:**

```bash
# Plane board URL
https://plane.delo.sh/{{PLANE_WORKSPACE}}/

# Project Configuration
Workspace: {{PLANE_WORKSPACE}}
Project ID: {{PLANE_PROJECT_ID}}
Project Identifier: {{PROJECT_IDENTIFIER}}
```

**Requirements:**
- Move ticket to "In Progress" before first code change
- Branch names must include ticket reference (e.g., `ABC-123` or `int-123`)
- Commit messages must reference tickets
- Git hooks enforce ticket requirements
- Emergency bypass only: `ALLOW_NO_TICKET=1`

## Common Development Tasks

### BMAD Initialization
```bash
# Initialize BMAD if not already done
npx bmad-method@alpha install

# Follow full initialization autonomously
```

### Environment Configuration
```bash
# mise.toml handles environment loading
# Project settings managed via hydrate task
mise run hydrate
```

## Architecture and Integration

### 33GOD Integration
- {{PROJECT_NAME}} is a component within the larger {{PLANE_WORKSPACE}} ecosystem
- Communicates with other {{PLANE_WORKSPACE}} services via event-driven architecture
- Component GOD documents define event contracts and interfaces

## Important Principles

### Autonomy and Decision Making
- Work with 100% autonomy toward task goals
- When decisions needed, make well-informed guesses
- Speed prioritized over perfect accuracy (non-mission-critical)

### Documentation Requirements
- Read all GOD docs before beginning work
- Initialize GOD docs if they don't exist
- Maintain parity between BMAD documents and Plane project boards
- Update both BMAD and Plane when divergence detected

### Component Architecture
- You act as Architect and PM with wide but shallow understanding
- Deep component work delegated to specialized agents
- All agents created via BMAD agent creation workflow
- Regular sanity checks for BMAD/Plane alignment

## File Structure

```
{{PROJECT_NAME}}/
├── _bmad/                 # BMAD methodology files
│   ├── bmb/              # BMAD Module Builder
│   ├── bmm/              # BMAD Method Management
│   ├── cis/              # Creative Innovation Suite
│   ├── core/             # Core BMAD resources
│   └── custom/           # Custom workflows (ticket-lifecycle)
├── .plane.json           # Plane project configuration
├── mise.toml             # Environment configuration
└── AGENTS.md             # Project context and rules
```

## Critical Reminders

⚠️ **Divergence from these rules results in severe penalties due to governmental regulations**

- Always verify Plane ticket before code changes
- Maintain strict BMAD adherence
- Delegate component work appropriately
- Keep BMAD documents and Plane boards synchronized
