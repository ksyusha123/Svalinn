import { createExperiment } from './workload-model.js';

// Характер работы выбирается параметром WORKLOAD: Cpu, Io, Mixed или Delay.
const experiment = createExperiment('LowFlood');
export const options = experiment.options;
export function runRequest() { return experiment.runRequest(); }
export function handleSummary(data) { return experiment.handleSummary(data); }
