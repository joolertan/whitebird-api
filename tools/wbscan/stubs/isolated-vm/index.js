export class Isolate {
  constructor() {
    throw new Error('wbscan does not evaluate bundle code (isolated-vm is stubbed out)');
  }
}

export default { Isolate };
