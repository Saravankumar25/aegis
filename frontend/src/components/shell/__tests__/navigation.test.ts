import { describe, expect, test } from 'vitest';
import { buildBreadcrumbs, relatedDestinations } from '../navigation';

describe('console navigation', () => {
  test('uses product labels and preserves a shortened incident identity', () => {
    expect(buildBreadcrumbs('/incidents/inc_01JABCDEFGH123')).toEqual([
      { href: '/incidents', label: 'Incidents' },
      { href: '/incidents/inc_01JABCDEFGH123', label: 'INC_01JABCDE' },
    ]);
  });

  test('keeps contextual destinations goal-oriented', () => {
    expect(relatedDestinations('/incidents/inc_123').map((item) => item.label)).toEqual([
      'Live Systems', 'Service Graph', 'Approvals',
    ]);
  });
});
