# vehicle.py
import pygame

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
        self.passengers = 4
        self.leg_state = "APPROACHING"
        self.intersection_entry_approaches = {}
        self.intersection_entry_movements = {}
        self.must_hold_for_lane = False
        self.merge_hold_distance = 35.0
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
                if 0.0 <= dist_to_stop <= 250.0:
                    # Check if a DBL bus is directly behind us pushing forward
                    is_car_ahead_of_bus = False
                    for other in all_vehicles:
                        if isinstance(other, Bus) and signal_controller.is_bus_dbl_eligible(other, target_node_x):
                            if self.direction == "EB" and other.x < self.x: is_car_ahead_of_bus = True
                            elif self.direction == "WB" and other.x > self.x: is_car_ahead_of_bus = True
                    
                    if not is_car_ahead_of_bus:
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
                if self.is_spillback_blocked(target_node_x, h_y, road_w, all_vehicles):
                    should_stop = True
                elif signal_controller and not signal_controller.request_intersection_entry(
                    self, target_node_x, all_vehicles
                ):
                    should_stop = True

            if self.must_hold_for_lane and 0.0 <= dist_to_stop <= self.merge_hold_distance:
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
    def __init__(self, x, y, direction, route_info, bus_id="BUS_01"):
        first_node_x = 300 if direction == "EB" else 700
        target_turn = route_info.get("waypoints", {}).get(first_node_x, "STRAIGHT")
        super().__init__(x=x, y=y, direction=direction, max_speed=1.0, color=(245, 158, 11), is_heavy=True, target_turn=target_turn, lane_index=(2 if target_turn == "LEFT" else 1))
        self.bus_id = bus_id
        self.route_info = route_info
        self.route_id = route_info.get("route_id", "")
        self.length, self.width, self.passengers = 42, 14, 45
        self.route_nodes = sorted(
            route_info.get("waypoints", {}).keys(),
            reverse=(direction == "WB"),
        )

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

    def is_target_lane_clear(self, desired_y, all_vehicles):
        corridor_min = min(self.y, desired_y) - self.width / 2.0
        corridor_max = max(self.y, desired_y) + self.width / 2.0
        for other in all_vehicles:
            if other is self: continue
            if other.direction == self.direction:
                other_min = other.y - other.width / 2.0
                other_max = other.y + other.width / 2.0
                lateral_overlap = other_min < corridor_max and other_max > corridor_min
                if lateral_overlap and abs(other.x - self.x) < (self.length + other.length) / 2.0 + 15:
                    return False
        return True

    def update(self, signal_data, int_x_list, h_y, road_w=132, stop_offset=10, lane_w=22, all_vehicles=None, signal_controller=None):
        if all_vehicles is None: all_vehicles = []
        leg = self.get_active_route_leg(int_x_list)
        target_node_x = leg["node_x"] if leg else self.get_next_target_node(int_x_list)
        self.target_turn = leg["movement"] if leg else "STRAIGHT"
        required_lane = leg["entry_lane"] if leg else self.lane_index
        self.must_hold_for_lane = False

        if self.target_turn == "LEFT" and self.lane_index != required_lane:
            dist_to_intersection = abs(self.x - target_node_x) if self.direction in ("EB", "WB") else abs(self.y - h_y)
            if dist_to_intersection < 250.0:
                desired_y = h_y - (2.5 * lane_w) if self.direction == "EB" else h_y + (2.5 * lane_w)
                if self.is_target_lane_clear(desired_y, all_vehicles):
                    if abs(self.y - desired_y) > 1.0: self.y += 0.5 if self.y < desired_y else -0.5
                    else:
                        self.y = desired_y
                        self.lane_index = required_lane
                if self.lane_index != required_lane:
                    self.must_hold_for_lane = True

        super().update(signal_data, int_x_list, h_y, road_w, stop_offset, lane_w, all_vehicles, signal_controller)

    def draw(self, screen):
        super().draw(screen)
        inner_rect = pygame.Rect(int(self.x - self.length / 4.0), int(self.y - self.width / 4.0), self.length / 2.0, self.width / 2.0) if self.direction in ("EB", "WB") else pygame.Rect(int(self.x - self.width / 4.0), int(self.y - self.length / 4.0), self.width / 2.0, self.length / 2.0)
        pygame.draw.rect(screen, (255, 255, 255), inner_rect, border_radius=1)
