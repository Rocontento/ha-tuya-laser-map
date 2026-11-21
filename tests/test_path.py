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


def test_current_room_from_last_point():
    from custom_components.tuya_laser_map.protocol.mapdata import Room
    cells = bytearray(100 * 100)
    for y in range(40, 60):
        for x in range(40, 60):
            cells[y * 100 + x] = (3 << 3) | 7  # room 3 floor
    c = LaserMapCoordinator.__new__(LaserMapCoordinator)
    c.vmap = VacuumMap(1, 100, 100, (50.0, 50.0), 5, (50.0, 50.0), bytes(cells), {3: Room(3, "Kitchen")})
    c.path = [[(0, 0), (20, -20)]]          # robot at cell (52, 52): inside room 3
    assert c.current_room == 3
    c.path = [[(0, 0), (120, 0)]]           # cell (62, 50): 3 cells outside, nearest room cell is room 3
    assert c.current_room == 3
    c.path = [[(0, 0), (400, 0)]]           # far away: unknown
    assert c.current_room is None
