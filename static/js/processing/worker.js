// A processing worker: runs tasks.js tasks sent by the pool (pool.js). Messages in:
// {id, name, args}; out: {id, result} or {id, error}. Large arrays travel by transfer.

import { newEnv, runTask } from './tasks.js';
import { OpfsStore } from './store.js';
import { LUCKY_TASKS } from './lucky-tasks.js';
import { DEVELOP_TASKS } from './develop-tasks.js';

const env = newEnv({ extraTasks: { ...LUCKY_TASKS, ...DEVELOP_TASKS } });

self.onmessage = async (event) => {
    const { id, name, args } = event.data;
    try {
        if (name === 'openStore') {
            env.store = args.storeId ? await OpfsStore.open(args.storeId) : null;
            self.postMessage({ id, result: { ok: true, kind: env.store ? env.store.kind : null } });
            return;
        }
        const { result, transfer } = await runTask(name, args, env);
        self.postMessage({ id, result }, transfer || []);
    } catch (error) {
        self.postMessage({ id, error: { message: error && error.message ? error.message : String(error), name: error && error.name } });
    }
};
