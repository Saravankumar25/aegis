import { describe, expect, test } from 'vitest';
import { sampleScenario, type SampleScenario } from '../sample-scenarios';

describe('sample scenarios', () => {
  test('are versioned, deterministic and contain no answer key fields', () => {
    const scenarios: SampleScenario[] = [{
      id: 'LAT-SN-001', version: 1, title: 'Latency', severity: 'P2',
      service: 'timeline', signal: 'latency', summary: 'p99 above baseline',
      learningGoal: 'Trace the symptom.',
    }];
    expect(sampleScenario(scenarios, 'LAT-SN-001')).toEqual(scenarios[0]);
    expect(JSON.stringify(scenarios)).not.toMatch(/ground_truth|root_cause|fault/i);
  });
});
