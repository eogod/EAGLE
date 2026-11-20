// Unified Kill Chain: https://www.unifiedkillchain.com/assets/The-Unified-Kill-Chain.pdf
export const UCKC_PHASES = [
  {
    id: 'reconnaissance',
    name: 'Reconnaissance',
    order: 1,
    desc: 'Researching and selecting targets',
  },
  {
    id: 'resource_development',
    name: 'Resource Development',
    order: 2,
    desc: 'Setting up attack infrastructure',
  },
  { id: 'delivery', name: 'Delivery', order: 3, desc: 'Transmission of weaponized object' },
  {
    id: 'social_engineering',
    name: 'Social Engineering',
    order: 4,
    desc: 'Manipulation of people',
  },
  { id: 'exploitation', name: 'Exploitation', order: 5, desc: 'Exploiting vulnerabilities' },
  { id: 'persistence', name: 'Persistence', order: 6, desc: 'Maintaining persistent presence' },
  { id: 'defense_evasion', name: 'Defense Evasion', order: 7, desc: 'Avoiding detection' },
  {
    id: 'command_and_control',
    name: 'Command & Control',
    order: 8,
    desc: 'Communication with controlled systems',
  },
  {
    id: 'pivoting',
    name: 'Pivoting',
    order: 9,
    desc: 'Tunneling traffic through controlled systems',
  },
  { id: 'discovery', name: 'Discovery', order: 10, desc: 'Gaining knowledge about systems' },
  {
    id: 'privilege_escalation',
    name: 'Privilege Escalation',
    order: 11,
    desc: 'Gaining higher permissions',
  },
  { id: 'execution', name: 'Execution', order: 12, desc: 'Execution of attacker-controlled code' },
  { id: 'credential_access', name: 'Credential Access', order: 13, desc: 'Accessing credentials' },
  {
    id: 'lateral_movement',
    name: 'Lateral Movement',
    order: 14,
    desc: 'Horizontal access to remote systems',
  },
  { id: 'collection', name: 'Collection', order: 15, desc: 'Identifying and gathering data' },
  { id: 'exfiltration', name: 'Exfiltration', order: 16, desc: 'Removing data from network' },
  { id: 'impact', name: 'Impact', order: 17, desc: 'Manipulating or destroying systems' },
  { id: 'objectives', name: 'Objectives', order: 18, desc: 'Achieving strategic goals' },
]

export const getPhaseByOrder = (order) => UCKC_PHASES.find((p) => p.order === order)

export const getPhaseById = (id) => UCKC_PHASES.find((p) => p.id === id)

export const getAvailablePhasesAfter = (afterOrder, existingOrders = []) => {
  const nextExisting = existingOrders.filter((o) => o > afterOrder).sort((a, b) => a - b)[0]

  const maxOrder = nextExisting ? nextExisting - 1 : UCKC_PHASES.length

  return UCKC_PHASES.filter(
    (p) => p.order > afterOrder && p.order <= maxOrder && !existingOrders.includes(p.order),
  )
}

export const getAvailablePhasesBefore = (beforeOrder, existingOrders = []) => {
  const prevExisting = existingOrders.filter((o) => o < beforeOrder).sort((a, b) => b - a)[0]
  const minOrder = prevExisting ? prevExisting + 1 : 1
  return UCKC_PHASES.filter(
    (p) => p.order >= minOrder && p.order < beforeOrder && !existingOrders.includes(p.order),
  )
}
