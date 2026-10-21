"""
Two robots swap sides: each drives to the other's starting point, a barrier certificate
keeping them apart as they pass. The run ends when both have arrived.

    robotarium simulate examples/two_robots --show
"""

import numpy as np

import rps.robotarium as robotarium
from rps.utilities.barrier_certificates import create_si_barrier_certificate_with_boundary
from rps.utilities.controllers import create_si_position_controller
from rps.utilities.misc import create_at_position
from rps.utilities.transformations import create_si_to_uni_mapping

from robotarium_runtime import RunLogger, config

CONFIG = config(default={"max_iterations": 1500})

N = 2
# Columns are robots: x, y, heading. They face each other across the arena's middle.
INITIAL = np.array([[-0.6, 0.6], [0.0, 0.0], [0.0, np.pi]])
GOALS = np.array([[0.6, -0.6], [0.0, 0.0]])

r = robotarium.Robotarium(number_of_robots=N, show_figure=True, sim_in_real_time=True,
                          initial_conditions=INITIAL)

si_to_uni, uni_to_si = create_si_to_uni_mapping(projection_distance=0.08)
controller = create_si_position_controller(velocity_magnitude_limit=0.1)
barrier = create_si_barrier_certificate_with_boundary(magnitude_limit=0.1)
arrived = create_at_position(position_error=0.05)

log = RunLogger("two_robots", autosave_every=300, meta={"goals": GOALS.tolist()})

for step in range(int(CONFIG["max_iterations"])):
    x = r.get_poses()
    xi = uni_to_si(x)
    if arrived(xi, GOALS)[0]:
        log.note("both robots arrived")
        r.step()
        break

    dxi = barrier(controller(xi, GOALS), xi)
    r.set_velocities(np.arange(N), si_to_uni(dxi, x))
    log.record(poses=x)
    r.step()

log.save()

# The Robotarium requires this call at the end of every experiment.
r.debug()
