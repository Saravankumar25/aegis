/**
 * Deterministic first-run material copied from the public alert portion of the
 * versioned evaluation scenarios. Ground truth and fault instructions are
 * deliberately absent: sample mode teaches the investigation workflow, not
 * the answer key.
 */
export type SampleScenario = {
  readonly id: string;
  readonly version: number;
  readonly title: string;
  readonly severity: 'P1' | 'P2' | 'P3' | 'P4';
  readonly service: string;
  readonly signal: string;
  readonly summary: string;
  readonly learningGoal: string;
};

export function sampleScenario(
  scenarios: readonly SampleScenario[], id: string,
): SampleScenario | undefined {
  return scenarios.find((scenario) => scenario.id === id);
}
