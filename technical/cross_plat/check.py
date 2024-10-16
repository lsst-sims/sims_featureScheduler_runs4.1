import numpy as np
from rubin_scheduler.scheduler.utils import restore_scheduler
from rubin_scheduler.scheduler.model_observatory import ModelObservatory
from cross import example_scheduler


if __name__ == "__main__":
    sched = example_scheduler()
    observatory = ModelObservatory()
    sched, obs = restore_scheduler(439, sched, observatory, 'cross_v4.1_0yrs.db')
    conditions = obs.return_conditions()
    rewards = []
    for sur in sched.survey_lists[4]:
        reward = np.nanmax(sur.calc_reward_function(conditions))
        print(sur, reward)
        rewards.append(reward)

    sched.update_conditions(conditions)
    print(sched.request_observation())
    ack = np.concatenate(sched.queue)
    print(ack["RA"], ack["dec"], ack["scheduler_note"])
