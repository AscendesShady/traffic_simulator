# vehicle.py
import pygame


CAR_PASSENGERS = 4
TRUCK_PASSENGERS = 1
DBL_LANE_INDEX = 2
# How long a bus may hold for a blocked DBL merge before giving the merge up
# for the current leg. 300 frames is about five seconds at 60 Hz.
DBL_MERGE_ABANDON_FRAMES = 300
# Speed at or below which a vehicle counts as stopped for blocking checks.
SLOW_VEHICLE_SPEED = 0.5
# A multi-leg bus that needs a different lane at the next node gets a short,
# deterministic merge area immediately after the node it just crossed.  The
# target-lane gap is checked before the bus enters that upstream node; if the
# gap disappears later, the bus waits at the end of this merge area instead of
# carrying an unresolved lane change to the next stop bar.
ROUTE_MERGE_AREA_PX = 80.0
ROUTE_MERGE_SAFE_GAP_PX = 15.0
ROUTE_MERGE_YIELD_DISTANCE_PX = 100.0


def corridor_blockers(mover, desired_y, all_vehicles):
    """Same-direction vehicles occupying `mover`'s lane-change corridor.

    Shared by the live merge check and by telemetry, so the obstruction the
    model is told about is the same one that actually stops a merge.
    """
    corridor_min = min(mover.y, desired_y) - mover.width / 2.0
    corridor_max = max(mover.y, desired_y) + mover.width / 2.0
    blockers = []
    for other in all_vehicles or []:
        if other is mover or other.direction != mover.direction:
            continue
        other_min = other.y - other.width / 2.0
        other_max = other.y + other.width / 2.0
        if not (other_min < corridor_max and other_max > corridor_min):
            continue
        if abs(other.x - mover.x) < (mover.length + other.length) / 2.0 + 15:
            blockers.append(other)
    return blockers


def dbl_lane_center_y(direction, h_y, lane_w=22):
    """Centre line of the DBL lane for an EB or WB approach."""
    offset = (DBL_LANE_INDEX + 0.5) * lane_w
    return h_y - offset if direction == "EB" else h_y + offset


def dbl_lane_is_obstructed(bus, all_vehicles, h_y, lane_w=22):
    """Whether `bus` currently could not complete a merge into the DBL lane.

    Only stopped or crawling traffic counts: a vehicle moving through the
    corridor at speed clears on its own and should not be reported to the
    model as an obstruction. A bus already in the DBL lane is never
    obstructed, since it no longer needs the merge.
    """
    if getattr(bus, "lane_index", None) == DBL_LANE_INDEX:
        return False
    desired_y = dbl_lane_center_y(bus.direction, h_y, lane_w)
    return any(
        other.speed <= SLOW_VEHICLE_SPEED
        for other in corridor_blockers(bus, desired_y, all_vehicles)
    )


def vehicle_is_inside_any_intersection(vehicle, int_x_list, h_y, road_w):
    """Conservative geometry check used to avoid asking an in-node car to yield."""
    half_w = road_w / 2.0
    if vehicle.direction in ("EB", "WB"):
        front = vehicle.x + vehicle.length / 2.0
        rear = vehicle.x - vehicle.length / 2.0
        return any(rear < node_x + half_w and front > node_x - half_w for node_x in int_x_list)
    front = vehicle.y + vehicle.length / 2.0
    rear = vehicle.y - vehicle.length / 2.0
    return rear < h_y + half_w and front > h_y - half_w


def should_yield_for_route_merge(vehicle, all_vehicles, int_x_list, h_y, road_w):
    """Return True when a target-lane vehicle should open a gap behind a bus.

    Only the vehicle behind the merging bus yields.  Vehicles ahead continue
    discharging and create the forward half of the gap.  A vehicle already in
    an intersection is never stopped by this cooperative rule.
    """
    if vehicle_is_inside_any_intersection(vehicle, int_x_list, h_y, road_w):
        return False

    for bus in all_vehicles or []:
        if bus is vehicle or not isinstance(bus, Bus):
            continue
        if not getattr(bus, "route_merge_active", False):
            continue
        if vehicle.direction != bus.direction:
            continue
        desired_y = getattr(bus, "route_merge_desired_y", None)
        if desired_y is None or abs(vehicle.y - desired_y) >= 8.0:
            continue

        if bus.direction == "EB":
            behind = vehicle.x < bus.x
        elif bus.direction == "WB":
            behind = vehicle.x > bus.x
        else:
            behind = False
        if not behind:
            continue

        bumper_gap = abs(vehicle.x - bus.x) - (
            vehicle.length + bus.length
        ) / 2.0
        if bumper_gap <= ROUTE_MERGE_YIELD_DISTANCE_PX:
            return True
    return False


class Vehicle:
    def __init__(self, x, y, direction, max_speed=1.0, color=(50, 150, 250), is_heavy=False, target_turn="STRAIGHT", lane_index=2, assigned_node_x=None):
        self.x = float(x)
        self.y = float(y)
        self.direction = direction
        self.max_speed = max_speed
        self.speed = max_speed
        self.color = color
        self.is_heavy = is_heavy
        self.lane_index = lane_index
        self.target_turn = target_turn
        self.passed_nodes = set()
        self.length = 28 if is_heavy else 18
        self.width = 12 if is_heavy else 10
        self.passengers = TRUCK_PASSENGERS if is_heavy else CAR_PASSENGERS
        self.leg_state = "APPROACHING"
        self.intersection_entry_approaches = {}
        self.intersection_entry_movements = {}
        self.must_hold_for_lane = False
        self.merge_hold_distance = 35.0
        self.route_merge_hold_active = False
        self.route_exit_merge_blocked = False
        # NB/SB traffic belongs to one physical vertical road. Pinning that
        # node prevents it from falsely "completing" the remote intersection,
        # which shares the same horizontal y-coordinate.
        self.assigned_node_x = assigned_node_x

    def get_next_target_node(self, int_x_list):
        sorted_nodes = sorted(int_x_list)
        if self.direction == "EB":
            upcoming = [nx for nx in sorted_nodes if nx not in self.passed_nodes]
            return upcoming[0] if upcoming else sorted_nodes[-1]
        elif self.direction == "WB":
            upcoming = [nx for nx in sorted_nodes if nx not in self.passed_nodes]
            return upcoming[-1] if upcoming else sorted_nodes[0]
        else:
            if self.assigned_node_x not in sorted_nodes:
                self.assigned_node_x = min(sorted_nodes, key=lambda cx: abs(self.x - cx))
            return self.assigned_node_x

    def get_lead_vehicle_distance(self, all_vehicles):
        if not all_vehicles: return float('inf')
        min_dist = float('inf')
        my_half_w = self.width / 2.0
        
        for other in all_vehicles:
            if other is self: continue
            
            if other.direction in ("EB", "WB"):
                o_min_y, o_max_y = other.y - other.width/2.0, other.y + other.width/2.0
                o_min_x, o_max_x = other.x - other.length/2.0, other.x + other.length/2.0
            else:
                o_min_y, o_max_y = other.y - other.length/2.0, other.y + other.length/2.0
                o_min_x, o_max_x = other.x - other.width/2.0, other.x + other.width/2.0
                
            if self.direction in ("EB", "WB"):
                if not (self.y + my_half_w <= o_min_y or self.y - my_half_w >= o_max_y):
                    if self.direction == "EB" and o_min_x > self.x:
                        dist = o_min_x - (self.x + self.length/2.0)
                        if 0 <= dist < min_dist: min_dist = dist
                    elif self.direction == "WB" and o_max_x < self.x:
                        dist = (self.x - self.length/2.0) - o_max_x
                        if 0 <= dist < min_dist: min_dist = dist
            elif self.direction in ("NB", "SB"):
                if not (self.x + my_half_w <= o_min_x or self.x - my_half_w >= o_max_x):
                    if self.direction == "NB" and o_max_y < self.y:
                        dist = (self.y - self.length/2.0) - o_max_y
                        if 0 <= dist < min_dist: min_dist = dist
                    elif self.direction == "SB" and o_min_y > self.y:
                        dist = o_min_y - (self.y + self.length/2.0)
                        if 0 <= dist < min_dist: min_dist = dist
                        
        return min_dist
        
    def is_front_bumper_upstream(self, target_node_x, h_y=300, road_w=132, stop_offset=10):
        half_w = road_w // 2
        fx = self.x + self.length / 2.0 if self.direction == "EB" else self.x - self.length / 2.0
        fy = self.y + self.length / 2.0 if self.direction == "SB" else self.y - self.length / 2.0

        if self.direction == "EB": return fx < (target_node_x - half_w - stop_offset)
        elif self.direction == "WB": return fx > (target_node_x + half_w + stop_offset)
        elif self.direction == "NB": return fy > (h_y + half_w + stop_offset)
        elif self.direction == "SB": return fy < (h_y - half_w - stop_offset)
        return False

    def distance_to_node_stop_bar(self, target_node_x, h_y=300, road_w=132, stop_offset=10):
        half_w = road_w // 2
        if self.direction == "EB": return (target_node_x - half_w - stop_offset) - (self.x + self.length / 2.0)
        elif self.direction == "WB": return (self.x - self.length / 2.0) - (target_node_x + half_w + stop_offset)
        elif self.direction == "NB": return (self.y - self.length / 2.0) - (h_y + half_w + stop_offset)
        elif self.direction == "SB": return (h_y - half_w - stop_offset) - (self.y + self.length / 2.0)
        return 0.0

    def is_spillback_blocked(self, target_node_x, h_y, road_w, all_vehicles):
        half_w = road_w // 2
        SAFE_GAP = 12.0
        
        for other in all_vehicles:
            if other is self: continue
            
            if self.target_turn == "STRAIGHT":
                if other.direction == self.direction:
                    if self.direction in ("EB", "WB") and abs(other.y - self.y) < 8:
                        if self.direction == "EB" and other.x > target_node_x:
                            tail_space = (other.x - other.length/2.0) - (target_node_x + half_w)
                            if tail_space < self.length + SAFE_GAP and other.speed < 0.5:
                                return True
                        elif self.direction == "WB" and other.x < target_node_x:
                            tail_space = (target_node_x - half_w) - (other.x + other.length/2.0)
                            if tail_space < self.length + SAFE_GAP and other.speed < 0.5:
                                return True
                    elif self.direction in ("NB", "SB") and abs(other.x - self.x) < 8:
                        if self.direction == "NB" and other.y < h_y:
                            tail_space = (h_y - half_w) - (other.y + other.length/2.0)
                            if tail_space < self.length + SAFE_GAP and other.speed < 0.5:
                                return True
                        elif self.direction == "SB" and other.y > h_y:
                            tail_space = (other.y - other.length/2.0) - (h_y + half_w)
                            if tail_space < self.length + SAFE_GAP and other.speed < 0.5:
                                return True
            elif self.target_turn == "LEFT":
                target_dir = {"EB":"NB", "WB":"SB", "NB":"WB", "SB":"EB"}[self.direction]
                if other.direction == target_dir:
                    lane_offset = 2.5 * 22
                    if self.direction == "EB" and abs(other.x - (target_node_x - lane_offset)) < 8:
                        if other.y < h_y:
                            tail_space = (h_y - half_w) - (other.y + other.length/2.0)
                            if tail_space < self.length + SAFE_GAP and other.speed < 0.5: return True
                    elif self.direction == "WB" and abs(other.x - (target_node_x + lane_offset)) < 8:
                        if other.y > h_y:
                            tail_space = (other.y - other.length/2.0) - (h_y + half_w)
                            if tail_space < self.length + SAFE_GAP and other.speed < 0.5: return True
                    elif self.direction == "NB" and abs(other.y - (h_y + lane_offset)) < 8:
                        if other.x < target_node_x:
                            tail_space = (target_node_x - half_w) - (other.x + other.length/2.0)
                            if tail_space < self.length + SAFE_GAP and other.speed < 0.5: return True
                    elif self.direction == "SB" and abs(other.y - (h_y - lane_offset)) < 8:
                        if other.x > target_node_x:
                            tail_space = (other.x - other.length/2.0) - (target_node_x + half_w)
                            if tail_space < self.length + SAFE_GAP and other.speed < 0.5: return True
        return False

    def update(self, signal_data, int_x_list, h_y, road_w=132, stop_offset=10, lane_w=22, all_vehicles=None, signal_controller=None):
        if all_vehicles is None: all_vehicles = []
        target_node_x = self.get_next_target_node(int_x_list)
        node_signals = signal_data.get(target_node_x, {})
        half_w = road_w // 2
        should_stop = False

        upstream = self.is_front_bumper_upstream(target_node_x, h_y, road_w, stop_offset)
        
        if upstream:
            self.leg_state = "APPROACHING"
        elif self.leg_state == "APPROACHING" and target_node_x not in self.passed_nodes:
            self.leg_state = "TURNING" if self.target_turn == "LEFT" else "IN_INTERSECTION"

        # 1. UPSTREAM DBL YIELDING (F-01 Fixed: Car yields only if BEHIND the priority bus)
        if not isinstance(self, Bus) and signal_controller:
            dbl_request = signal_controller.get_active_dbl_request(
                target_node_x, self.direction
            )
            if upstream and dbl_request and self.lane_index == dbl_request["entry_lane"]:
                dist_to_stop = self.distance_to_node_stop_bar(target_node_x, h_y, road_w, stop_offset)
                eligibility_px = signal_controller.get_priority_eligibility_px()
                if 0.0 <= dist_to_stop <= eligibility_px:
                    # Check if a DBL bus is directly behind us pushing forward
                    is_car_ahead_of_bus = False
                    for other in all_vehicles:
                        if isinstance(other, Bus) and signal_controller.is_bus_dbl_eligible(other, target_node_x):
                            if self.direction == "EB" and other.x < self.x: is_car_ahead_of_bus = True
                            elif self.direction == "WB" and other.x > self.x: is_car_ahead_of_bus = True
                    
                    if not is_car_ahead_of_bus:
                        should_stop = True

        # A bus changing lanes between route legs owns a small cooperative
        # merge gap.  Only traffic behind it in the target lane yields; traffic
        # ahead keeps moving so this cannot freeze both sides of the gap.
        if should_yield_for_route_merge(
            self, all_vehicles, int_x_list, h_y, road_w
        ):
            should_stop = True

        # 2. SIGNAL YIELDING & DOWNSTREAM SPILLBACK
        if target_node_x not in self.passed_nodes and upstream:
            dist_to_stop = self.distance_to_node_stop_bar(target_node_x, h_y, road_w, stop_offset)
            sig_state = node_signals.get(self.direction, "RED")
            if not isinstance(sig_state, str) or sig_state.upper() not in {
                "RED", "YELLOW", "GREEN"
            }:
                sig_state = "RED"
            else:
                sig_state = sig_state.upper()

            if sig_state != "GREEN" and signal_controller:
                signal_controller.cancel_intersection_entry(self, target_node_x)
            
            if sig_state in ("RED", "YELLOW") and dist_to_stop <= 15.0:
                should_stop = True
                
            if sig_state == "GREEN" and 0.0 <= dist_to_stop <= 25.0:
                if self.route_exit_merge_blocked:
                    should_stop = True
                elif self.is_spillback_blocked(target_node_x, h_y, road_w, all_vehicles):
                    should_stop = True
                elif signal_controller and not signal_controller.request_intersection_entry(
                    self, target_node_x, all_vehicles
                ):
                    should_stop = True

            if self.must_hold_for_lane and 0.0 <= dist_to_stop <= self.merge_hold_distance:
                should_stop = True

        # This hold point is on the link immediately after the previous node,
        # not at the next intersection. It keeps a bus with an unresolved
        # route-required merge out of the next node's middle-lane queue.
        if self.route_merge_hold_active:
            should_stop = True

        # 3. KINEMATICS
        lead_dist = max(0.0, self.get_lead_vehicle_distance(all_vehicles))
        SAFE_GAP = 12.0

        if lead_dist < SAFE_GAP or should_stop:
            should_stop = True
            self.speed = 0.0
        elif lead_dist < SAFE_GAP + 25.0:
            target_speed = min(self.max_speed, (lead_dist / 30.0) * self.max_speed)
            self.speed = max(0.0, self.speed - 0.05) if self.speed > target_speed else self.speed
        else:
            self.speed = min(self.max_speed, self.speed + 0.05)

        if should_stop and upstream and signal_controller:
            signal_controller.cancel_intersection_entry(self, target_node_x)

        # 4. TURN TRIGGERS & CONTINUOUS MOVEMENT
        step_dist = min(self.speed, lead_dist) if lead_dist < float('inf') else self.speed
        turned = False

        if self.target_turn == "LEFT" and target_node_x not in self.passed_nodes:
            lane_offset = 2.5 * lane_w
            
            if isinstance(self, Bus) and self.lane_index != 2 and not upstream:
                self.speed = 0.0
                step_dist = 0.0
            else:
                if self.direction == "EB":
                    pivot_x = target_node_x - lane_offset
                    if self.x <= pivot_x and (self.x + step_dist) >= pivot_x:
                        overshoot = (self.x + step_dist) - pivot_x
                        self.x = pivot_x
                        self.direction = "NB"
                        self.assigned_node_x = target_node_x
                        self.y -= overshoot
                        turned = True
                        step_dist = 0.0
                elif self.direction == "WB":
                    pivot_x = target_node_x + lane_offset
                    if self.x >= pivot_x and (self.x - step_dist) <= pivot_x:
                        overshoot = pivot_x - (self.x - step_dist)
                        self.x = pivot_x
                        self.direction = "SB"
                        self.assigned_node_x = target_node_x
                        self.y += overshoot
                        turned = True
                        step_dist = 0.0
                elif self.direction == "NB":
                    pivot_y = h_y + lane_offset
                    if self.y >= pivot_y and (self.y - step_dist) <= pivot_y:
                        overshoot = pivot_y - (self.y - step_dist)
                        self.y = pivot_y
                        self.direction = "WB"
                        self.assigned_node_x = target_node_x
                        self.x -= overshoot
                        turned = True
                        step_dist = 0.0
                elif self.direction == "SB":
                    pivot_y = h_y - lane_offset
                    if self.y <= pivot_y and (self.y + step_dist) >= pivot_y:
                        overshoot = (self.y + step_dist) - pivot_y
                        self.y = pivot_y
                        self.direction = "EB"
                        self.assigned_node_x = target_node_x
                        self.x += overshoot
                        turned = True
                        step_dist = 0.0

        if step_dist > 0.0:
            if self.direction == "EB": self.x += step_dist
            elif self.direction == "WB": self.x -= step_dist
            elif self.direction == "NB": self.y -= step_dist
            elif self.direction == "SB": self.y += step_dist

        if turned:
            self.leg_state = "DEPARTING"

        rx = self.x - self.length / 2.0 if self.direction == "EB" else self.x + self.length / 2.0
        ry = self.y - self.length / 2.0 if self.direction == "SB" else self.y + self.length / 2.0

        conflict_cleared = False
        if self.direction == "EB" and rx > target_node_x + half_w: conflict_cleared = True
        elif self.direction == "WB" and rx < target_node_x - half_w: conflict_cleared = True
        elif self.direction == "NB" and ry < h_y - half_w: conflict_cleared = True
        elif self.direction == "SB" and ry > h_y + half_w: conflict_cleared = True

        if conflict_cleared and target_node_x not in self.passed_nodes:
            self.passed_nodes.add(target_node_x)
            self.leg_state = "COMPLETE"
            if not getattr(self, "is_heavy", False):
                self.target_turn = "STRAIGHT"

    def draw(self, screen):
        rect = pygame.Rect(int(self.x - self.length / 2.0), int(self.y - self.width / 2.0), self.length, self.width) if self.direction in ("EB", "WB") else pygame.Rect(int(self.x - self.width / 2.0), int(self.y - self.length / 2.0), self.width, self.length)
        pygame.draw.rect(screen, self.color, rect, border_radius=3)


class Bus(Vehicle):
    def __init__(
        self, x, y, direction, route_info, bus_id="BUS_01", max_speed=1.0
    ):
        first_node_x = 300 if direction == "EB" else 700
        target_turn = route_info.get("waypoints", {}).get(first_node_x, "STRAIGHT")
        super().__init__(
            x=x,
            y=y,
            direction=direction,
            max_speed=max_speed,
            color=(245, 158, 11),
            is_heavy=True,
            target_turn=target_turn,
            lane_index=(2 if target_turn == "LEFT" else 1),
        )
        self.bus_id = bus_id
        self.route_info = route_info
        self.route_id = route_info.get("route_id", "")
        self.length, self.width, self.passengers = 42, 14, 45
        self.route_nodes = sorted(
            route_info.get("waypoints", {}).keys(),
            reverse=(direction == "WB"),
        )
        # Per-leg DBL merge tracking. See the invariant in update().
        self.dbl_merge_hold_frames = 0
        self.dbl_merge_abandoned_for_leg = False
        self._dbl_merge_leg_key = None
        self.route_merge_active = False
        self.route_merge_desired_y = None

    def get_active_route_leg(self, int_x_list):
        """Return canonical metadata for the next unfinished route leg."""
        for route_leg_index, node_x in enumerate(self.route_nodes):
            if node_x in self.passed_nodes:
                continue
            movement = self.route_info.get("waypoints", {}).get(node_x, "STRAIGHT")
            configured_lanes = self.route_info.get("lanes", {})
            entry_lane = int(configured_lanes.get(node_x, 2 if movement == "LEFT" else 1))
            approach = self.direction
            if movement == "LEFT":
                exit_direction = {
                    "EB": "NB", "WB": "SB", "NB": "WB", "SB": "EB"
                }[approach]
            else:
                exit_direction = approach
            return {
                "route_leg_index": route_leg_index,
                "node_x": node_x,
                "approach": approach,
                "movement": movement,
                "entry_lane": entry_lane,
                "exit_direction": exit_direction,
            }
        return None

    def get_following_route_leg(self, leg):
        """Return the configured leg after ``leg``, if this route has one."""
        if not leg:
            return None
        route_leg_index = leg["route_leg_index"] + 1
        if route_leg_index >= len(self.route_nodes):
            return None
        node_x = self.route_nodes[route_leg_index]
        movement = self.route_info.get("waypoints", {}).get(node_x, "STRAIGHT")
        configured_lanes = self.route_info.get("lanes", {})
        return {
            "route_leg_index": route_leg_index,
            "node_x": node_x,
            "movement": movement,
            "entry_lane": int(
                configured_lanes.get(node_x, 2 if movement == "LEFT" else 1)
            ),
        }

    def route_merge_point_x(self, previous_node_x, road_w):
        """Centre of the post-node area reserved for a required lane merge."""
        clearance = road_w / 2.0 + self.length / 2.0 + ROUTE_MERGE_AREA_PX
        if self.direction == "EB":
            return previous_node_x + clearance
        return previous_node_x - clearance

    def target_lane_has_merge_storage(
        self, previous_node_x, target_lane, h_y, road_w, lane_w, all_vehicles
    ):
        """Check a bus-sized gap at the deterministic post-node merge point."""
        lane_offset = (target_lane + 0.5) * lane_w
        desired_y = h_y - lane_offset if self.direction == "EB" else h_y + lane_offset
        merge_x = self.route_merge_point_x(previous_node_x, road_w)
        for other in all_vehicles or []:
            if other is self or other.direction != self.direction:
                continue
            if abs(other.y - desired_y) >= 8.0:
                continue
            required_gap = (
                self.length + other.length
            ) / 2.0 + ROUTE_MERGE_SAFE_GAP_PX
            if abs(other.x - merge_x) < required_gap:
                return False
        return True

    def reached_route_merge_hold_point(self, previous_node_x, road_w):
        merge_x = self.route_merge_point_x(previous_node_x, road_w)
        if self.direction == "EB":
            return self.x >= merge_x
        return self.x <= merge_x

    def blocker_is_ahead(self, blocker):
        if self.direction == "EB":
            return blocker.x > self.x
        if self.direction == "WB":
            return blocker.x < self.x
        return False

    def is_target_lane_clear(self, desired_y, all_vehicles):
        return not corridor_blockers(self, desired_y, all_vehicles)

    def update(self, signal_data, int_x_list, h_y, road_w=132, stop_offset=10, lane_w=22, all_vehicles=None, signal_controller=None):
        if all_vehicles is None: all_vehicles = []
        leg = self.get_active_route_leg(int_x_list)
        target_node_x = leg["node_x"] if leg else self.get_next_target_node(int_x_list)
        self.target_turn = leg["movement"] if leg else "STRAIGHT"
        required_lane = leg["entry_lane"] if leg else self.lane_index
        self.must_hold_for_lane = False
        self.route_merge_hold_active = False
        self.route_exit_merge_blocked = False
        self.route_merge_active = False
        self.route_merge_desired_y = None

        # Before entering the current node, reserve physical storage for a
        # lane change required by the following route leg.  R2/R4 therefore do
        # not cross the first node into a link whose turn lane has no bus-sized
        # gap.  This is route geometry and remains active with DBL/TSP off.
        following_leg = self.get_following_route_leg(leg)
        if (
            leg
            and following_leg
            and following_leg["entry_lane"] != leg["entry_lane"]
        ):
            self.route_exit_merge_blocked = not self.target_lane_has_merge_storage(
                leg["node_x"],
                following_leg["entry_lane"],
                h_y,
                road_w,
                lane_w,
                all_vehicles,
            )

        # DBL merge state is tracked per leg: a lane blocked at one node must
        # not disable DBL for the rest of the route, where it may be clear.
        leg_key = (leg["node_x"], leg["route_leg_index"]) if leg else None
        if leg_key != self._dbl_merge_leg_key:
            self._dbl_merge_leg_key = leg_key
            self.dbl_merge_hold_frames = 0
            self.dbl_merge_abandoned_for_leg = False

        dist_to_intersection = (
            abs(self.x - target_node_x)
            if self.direction in ("EB", "WB")
            else abs(self.y - h_y)
        )
        dbl_enabled_for_leg = bool(
            leg
            and signal_controller
            and signal_controller.is_dbl_enabled_for_bus_leg(self, target_node_x)
        )
        turn_lane_change_due = (
            self.target_turn == "LEFT" and dist_to_intersection < 250.0
        )
        route_leg_merge_due = bool(
            leg
            and leg["route_leg_index"] > 0
            and self.lane_index != required_lane
        )

        # INVARIANT: DBL must never leave a bus worse off than no DBL at all.
        # A merge that stays blocked past DBL_MERGE_ABANDON_FRAMES is given up
        # for this leg, and the bus runs in its configured lane exactly as an
        # unequipped bus would, rather than holding upstream indefinitely.
        if not dbl_enabled_for_leg:
            self.dbl_merge_hold_frames = 0
            self.dbl_merge_abandoned_for_leg = False
        dbl_merge_due = dbl_enabled_for_leg and not self.dbl_merge_abandoned_for_leg

        # A DBL-enabled bus occupies the continuous outer lane as early as
        # traffic permits. Close to a left turn, its configured turn lane wins
        # if that lane ever differs from the DBL lane.
        if turn_lane_change_due:
            target_lane = required_lane
        elif dbl_merge_due:
            target_lane = DBL_LANE_INDEX
        elif route_leg_merge_due:
            target_lane = required_lane
        else:
            target_lane = required_lane
        lane_change_due = (
            dbl_merge_due or turn_lane_change_due or route_leg_merge_due
        ) and self.lane_index != target_lane
        if lane_change_due:
            lane_offset = (target_lane + 0.5) * lane_w
            desired_y = (
                h_y - lane_offset if self.direction == "EB" else h_y + lane_offset
            )
            lane_clear = self.is_target_lane_clear(desired_y, all_vehicles)
            route_required_targeted = (
                route_leg_merge_due and target_lane == required_lane
            )
            if route_required_targeted:
                self.route_merge_active = True
                self.route_merge_desired_y = desired_y
            if lane_clear:
                if abs(self.y - desired_y) > 1.0:
                    self.y += 0.5 if self.y < desired_y else -0.5
                else:
                    self.y = desired_y
                    self.lane_index = target_lane
            if self.lane_index != target_lane:
                self.must_hold_for_lane = True

            if route_required_targeted and self.lane_index != target_lane:
                previous_node_x = self.route_nodes[leg["route_leg_index"] - 1]
                blockers = corridor_blockers(self, desired_y, all_vehicles)
                # A follower in the target lane is asked to yield and the bus
                # may continue forward to open the gap.  A blocker ahead means
                # downstream storage is unavailable, so stop at the merge-area
                # boundary rather than queueing in the next node's middle lane.
                blocked_ahead = any(self.blocker_is_ahead(item) for item in blockers)
                if blocked_ahead and self.reached_route_merge_hold_point(
                    previous_node_x, road_w
                ):
                    self.route_merge_hold_active = True

            # Only a DBL-driven merge may be abandoned. A LEFT turn genuinely
            # needs its turn lane, and a leg whose configured lane already is
            # the DBL lane would block identically with DBL switched off, so
            # neither case is one that DBL made worse.
            abandonable = (
                dbl_merge_due
                and not turn_lane_change_due
                and not route_required_targeted
                and required_lane != DBL_LANE_INDEX
            )
            if abandonable and not lane_clear:
                self.dbl_merge_hold_frames += 1
                if self.dbl_merge_hold_frames > DBL_MERGE_ABANDON_FRAMES:
                    self.dbl_merge_abandoned_for_leg = True
                    # Settle fully back into the configured lane so the bus
                    # stops holding and the priority feasibility gate, which
                    # compares lane_index against the request entry lane, can
                    # still grant TSP to a bus that never got its DBL merge.
                    self.must_hold_for_lane = False
                    normal_offset = (required_lane + 0.5) * lane_w
                    self.y = (
                        h_y - normal_offset
                        if self.direction == "EB"
                        else h_y + normal_offset
                    )
                    self.lane_index = required_lane

        if dbl_enabled_for_leg and self.lane_index == DBL_LANE_INDEX:
            self.dbl_merge_hold_frames = 0

        super().update(signal_data, int_x_list, h_y, road_w, stop_offset, lane_w, all_vehicles, signal_controller)

    def draw(self, screen):
        super().draw(screen)
        inner_rect = pygame.Rect(int(self.x - self.length / 4.0), int(self.y - self.width / 4.0), self.length / 2.0, self.width / 2.0) if self.direction in ("EB", "WB") else pygame.Rect(int(self.x - self.width / 4.0), int(self.y - self.length / 4.0), self.width / 2.0, self.length / 2.0)
        pygame.draw.rect(screen, (255, 255, 255), inner_rect, border_radius=1)
