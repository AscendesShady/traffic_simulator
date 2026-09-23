# SUMO quick tutorial: what to use it for

SUMO is a microscopic and mesoscopic traffic-simulation toolkit for building or
importing road networks, generating demand, routing travelers, running traffic,
controlling it programmatically, and exporting measurements.

## Before you start

1. Install or build SUMO, then confirm `sumo --version`, `sumo-gui --version`, and `netedit --version` work in PowerShell.
2. Set the installation location with `$env:SUMO_HOME = "C:\Program Files (x86)\Eclipse\Sumo"` and add its executables with `$env:Path += ";$env:SUMO_HOME\bin"` if they are not already on `PATH`.
3. Add the Python tools with `$env:PYTHONPATH = "$env:SUMO_HOME\tools;$env:PYTHONPATH"` when using `traci`, `sumolib`, or scripts such as `randomTrips.py`.

This repository checkout currently contains source and launcher scripts but no
built SUMO executables in `bin/`, so build SUMO or point `SUMO_HOME` at an
installed distribution before running the commands below.

## First simulation in one-line steps

1. Generate a small grid network: `netgenerate --grid --grid.number 5 -o tutorial.net.xml`.
2. Generate one hour of random trips and valid routes: `python "$env:SUMO_HOME\tools\randomTrips.py" -n tutorial.net.xml -r tutorial.rou.xml -b 0 -e 3600 --period 1`.
3. Open and visually inspect the scenario: `sumo-gui -n tutorial.net.xml -r tutorial.rou.xml -b 0 -e 3600`.
4. Press the GUI Play button to run, use the delay control to change animation speed, and click vehicles or signals to inspect their state.
5. Save a reusable configuration: `sumo -n tutorial.net.xml -r tutorial.rou.xml -b 0 -e 3600 --save-configuration tutorial.sumocfg`.
6. Reopen the saved scenario: `sumo-gui -c tutorial.sumocfg`.
7. Run the same scenario without a GUI: `sumo -c tutorial.sumocfg`.
8. Export trip and run summaries: `sumo -c tutorial.sumocfg --tripinfo-output tripinfo.xml --summary-output summary.xml`.
9. Export every simulated vehicle's trajectory: `sumo -c tutorial.sumocfg --fcd-output trajectories.xml`.
10. Re-run with the same inputs, options, SUMO version, and random seed when you need a comparable experiment.

## Use SUMO for these workflows

| Goal | One-line instruction |
|---|---|
| Draw a road network | Run `netedit --new -o network.net.xml`, draw junctions/edges, define connections or signals, validate, and save. |
| Edit an existing scenario | Run `netedit network.net.xml` and use the Network, Demand, Data, and additional-element modes. |
| Import OpenStreetMap | Download an OSM extract, then run `netconvert --osm-files city.osm.xml -o city.net.xml` and inspect the result in Netedit. |
| Convert other network data | Use `netconvert` with the applicable importer for OpenDRIVE, VISUM, MATSim, shapefiles, or SUMO plain XML. |
| Create synthetic networks | Use `netgenerate` for grid, spider, and randomized test networks. |
| Generate traffic demand | Use `tools/randomTrips.py`, write trips/flows/persons in route XML, or import measured/OD demand. |
| Compute valid routes | Run `duarouter -n network.net.xml -r trips.trips.xml -o routes.rou.xml`. |
| Assign OD demand | Use `od2trips` to expand OD matrices and `duarouter`/assignment tools to route the resulting demand. |
| Model turn-ratio demand | Use `jtrrouter` when traffic is described by source flows and junction turning percentages. |
| Reconstruct flow from detectors | Use `dfrouter` with detector locations and measured flows. |
| Run large aggregate scenarios | Add `--mesosim` when segment/queue resolution is appropriate and microscopic lane detail is unnecessary. |
| Model cars and road traffic | Configure vehicle types, car-following, lane-changing, junction priority, speed limits, routes, stops, and departure behavior. |
| Model pedestrians and transit | Add persons, walking/ride stages, stops, lines, schedules, buses, trains, taxis, and intermodal routes. |
| Study traffic signals | Define fixed or actuated programs, detector inputs, offsets, coordination, or adaptive control. |
| Model rail operations | Use rail signals, crossings, route-dependent driveways, schedules, and rail constraints. |
| Study electric mobility | Attach battery/station-finder devices and define charging stations, overhead wires, or traction substations. |
| Measure traffic | Add induction loops, lane-area detectors, multi-entry/exit detectors, edge/lane mean data, or queue outputs. |
| Estimate emissions | Select emission classes and export emissions, fuel, electricity, noise, or trajectory data. |
| Control a live simulation | Use TraCI from Python, Java, C++, or another client to step the simulation and read/change objects. |
| Embed SUMO in a process | Use libsumo for a TraCI-shaped API without the socket/server boundary. |
| Analyze networks and XML | Use Python `sumolib` to read networks, routes, shapes, outputs, and call SUMO tools. |
| Visualize and post-process | Use `sumo-gui`, `tools/visualization/`, `traceExporter.py`, or your own XML/CSV/Parquet analysis. |
| Calibrate a model | Compare detector/travel-time outputs with observations and iteratively adjust demand and behavioral parameters. |
| Test control or routing research | Run repeatable scenarios across seeds/configurations and compare safety, delay, throughput, emissions, and fairness metrics. |

## Minimal Python control with TraCI

Create `controller.py`:

```python
import traci

traci.start(["sumo", "-c", "tutorial.sumocfg"])
while traci.simulation.getMinExpectedNumber() > 0:
    traci.simulationStep()
    # Read or change vehicles, lanes, signals, routes, and other domains here.
traci.close()
```

Run it with `python controller.py`; replace `"sumo"` with `"sumo-gui"` while
debugging visually.

## Typical project files

| File | Purpose |
|---|---|
| `*.net.xml` | computed simulation network with lanes, connections, junction logic, and traffic lights |
| `*.rou.xml` | vehicle types, routes, trips, flows, persons, and transport plans |
| `*.add.xml` | detectors, stops, rerouters, charging infrastructure, shapes, and other additions |
| `*.sumocfg` | scenario configuration joining inputs, time settings, processing options, and outputs |
| output XML/CSV/Parquet | trips, trajectories, detector values, emissions, statistics, and custom observations |

## Recommended working sequence

1. Define the question and measurable outputs before building the scenario.
2. Build or import the network and inspect connections, permissions, geometry, and signal-link mappings.
3. Create demand with realistic vehicle/person types, departures, routes, stops, and distributions.
4. Run a short GUI scenario to catch invalid routes, blocked connections, insertion failures, and unrealistic behavior.
5. Add detectors and outputs that directly measure the research or operational question.
6. Calibrate demand and behavior against observations before interpreting predictions.
7. Automate repeatable headless runs, multiple seeds, and parameter variations.
8. Preserve the network, demand, configuration, SUMO version, seed, and analysis code with every reported result.

## What SUMO does not guarantee

- A simulation is not automatically a calibrated prediction of real traffic.
- Imported networks usually need connection, permission, signal, and geometry review.
- One random seed or one demand realization is not sufficient for stochastic conclusions.
- Collision-free default behavior does not prove every custom controller or externally commanded maneuver is safe.
- GUI animation is a view of model state, not field evidence.
- Microscopic and mesoscopic modes answer different levels of detail and should not be treated as numerically interchangeable.

## Where to learn the internals in this knowledge base

- Start with `00_PROJECT_OVERVIEW.md` and `01_SYSTEM_ARCHITECTURE.md`.
- Follow a normal run through `03_EXECUTION_FLOW.md` and `04_DATA_FLOW.md`.
- Find a behavior in `07_ALGORITHM_INDEX.md` and its implementation in `source_maps/`.
- Review safe modification boundaries in `09_INVARIANTS.md`, `12_CHANGE_IMPACT.md`, and `14_REIMPLEMENTATION_SPEC.md`.
- Check documented limitations and evidence in `DOCUMENTATION_QA.md`.

## Confidence

High for the workflow and tool boundaries. Exact option availability and output
fields should always be checked with the installed SUMO version's `--help` and
the scenario-specific validation/tests.
