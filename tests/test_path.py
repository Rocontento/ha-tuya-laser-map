"""Path segments: pause/resume keeps the earlier path; a new job from the dock starts over."""

from custom_components.tuya_laser_map.coordinator import LaserMapCoordinator
from custom_components.tuya_laser_map.protocol.mapdata import VacuumMap


def test_path_accumulates_until_new_job():
    c = LaserMapCoordinator.__new__(LaserMapCoordinator)
    c.path = []
    c.vmap = VacuumMap(1, 100, 100, (50.0, 50.0), 5, (50.0, 50.0), bytes(100 * 100))  # dock at robot (0, 0)
    c._add_path([(0, 0), (100, 0)])                      # job starts at the dock
    c._add_path([(0, 0), (100, 0), (100, 500)])          # same file growing
    assert c.path == [[(0, 0), (100, 0), (100, 500)]]
    c._add_path([(120, 520), (300, 520)])                # resumed after a pause: new file mid-room
    assert len(c.path) == 2 and c.path[0][-1] == (100, 500)
    c._add_path([(5, 5), (50, 0)])                       # new job from the dock
    assert c.path == [[(5, 5), (50, 0)]]
