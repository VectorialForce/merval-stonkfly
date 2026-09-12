# What is actually modeled

This is a wiring-constrained spiking-network experiment. The connectome supplies anatomy, not a complete living fly, calibrated physiology, or a trading strategy. No LLM selects trades. No price rule overrides the neural proposal with a different action.

## Anatomy and dynamics

The [MaleCNS v1.0 release](https://male-cns.janelia.org/download/) provides the male brain and ventral nerve cord. Import retains every assigned neuronal superclass, including uncertain classes, while excluding explicit glia and unresolved segmentation objects. It retains all released edges between those entries, including weak edges and self-connections: **166,700 nodes, 25,582,938 directed connections, 124,177,617 synaptic contacts**. “Full retained” describes this inclusion policy; it does not mean every biological synapse was reconstructed.

The importer verifies SHA-256 source files and every compiled graph array against committed locks. Transmitter annotations and cell order are checked too. Neuron IDs remain integers. Source files are downloaded separately under their upstream license.

The native kernel integrates approximate leaky integrate-and-fire cells at **0.1 ms**. It uses 20 ms membrane and 5 ms synaptic time constants, a −45 mV threshold, 1.8 ms transmission delay and 2.2 ms refractory period. Contact count times 0.275 sets initial synaptic magnitude. ACh is assigned excitation; GABA, glutamate and histamine inhibition, with explicit positive fallback for unresolved signs. This is a coarse sign proxy, not receptor-specific physiology. KC rest is −60 mV with an 8 mV adaptation increment decaying over 200 ms; other cells rest at −52 mV.

Pure dopamine, serotonin and octopamine annotations deliver modulatory traces along their retained edges instead of generic fast excitation. Only the specified memory rule consumes selected dopamine activity; most modulatory effects are unmodeled. Cotransmission and receptors remain incomplete. Keeping an edge in the graph does not establish that all its biological effects are reproduced.

The event-driven kernel avoids unnecessary subthreshold updates; it does not prune the graph or enlarge the integration timestep. This accelerates model execution, not biological time.

## What the fly sees

PPI historical bars (`MarketData/Search`) initialize the price history. Subsequent observation midpoints are appended. A fixed 320×180 chart shows past prices, ticker name and current bid/ask; the simulator receives its **RGB pixels**, not raw prices or indicators. The chart is locally rendered, not a capture of a logged-in PPI account. It excludes portfolio balances and P&L.

3,335 mapped R1–R6 cells receive linear-sRGB luminance; 811 mapped R8 cells receive blue/green proxies. Sample locations are inferred from contacts with column-annotated visual cells, using overlapping left/right viewports. Unmapped receptors get no invented optical input. Photoreceptors and lamina are graded in real flies; using spikes, RGB channels, saturating current and a 12 mV-equivalent lamina bias is an explicit display adapter, not validated retinal physiology.

Existing R8→aMe12 connections use a net excitatory sign motivated by [Xiao et al., 2023](https://doi.org/10.1038/s41586-023-06681-6); transferring that result to these reconstructed cells and contact-count magnitudes remains an assumption. The initial dark chart barely activated KCs in our probe. We changed the chart to a light background, without changing neural currents or fitting to trading returns. Display sensitivity is a major confound to test.

By default each market observation advances **500 ms of neural time**, regardless of elapsed wall time. Wall observations are at least 60 seconds apart. That is a deliberately compressed market-to-neural clock, not real-time fly physiology. Eligibility and decay operate in neural seconds. Multiple optional assets are presented in a fixed round-robin schedule; the network does not choose which asset is shown.

## How a neural spike becomes an order

Over each observation, mean right DNp20 firing minus mean left DNp20 firing is decoded as follows:

| Neural measurement | Proposal |
| --- | --- |
| Difference ≥ 2 Hz, with at least one DNpe017 spike | Buy |
| Difference ≤ −2 Hz, with at least one DNpe017 spike | Sell |
| Otherwise | Hold |

This is an engineered interface, not a discovery of “buy neurons.” The mapping is fixed and reads only spike counts. Selected cell IDs appear in the local audit log. Persistent turning-like network bias can therefore become persistent buying; do not interpret that as market insight.

The guard can reject a proposal for price, budget, inventory, timing or account-state reasons. It cannot replace the proposal or manufacture a profitable policy. PPI's official `ppi-client` SDK bridges the separate [PPI REST API](https://itatppi.github.io/ppi-official-api-docs/api/documentacionRest/); there is no general-purpose trading/wallet framework in between.

## What changes with profit and loss

At the next observation, equity is cash plus holdings marked at the current bid. Its change since the last observation includes booked fees and unrealized price changes. A change of at least +10 ARS schedules a **200 ms, 20 mV-equivalent** artificial current into all **15 PAM11 (α1)** cells. A change of at most −10 ARS schedules the same pulse into the **two PPL101 (γ1pedc)** cells. The pulse is binary above the threshold, not proportional to profit. The deadband is per observation; tiny changes are not accumulated into a later pulse.

This is feedback about portfolio value, not evidence that the latest action caused that change. Holding an asset can produce either signal. Fees count as a loss. Deposits or unexplained balance changes halt execution instead of becoming rewards. Positive P&L need not be realized profit.

The candidate memory rule acts on **7,835 existing KC→MBON07/MBON11 edges**. It adapts a baseline-centered anti-Hebbian rate rule from [Huang, Luo et al., 2024](https://doi.org/10.1038/s41586-024-07819-w): recent KC activity followed by dopamine tends to depress eligible connections; the reverse timing can potentiate them. Actual network spikes supply KC/DAN rates in bins of at most 10 ms. No price or profit value directly edits a synaptic weight.

The 1-second eligibility traces, 1,800-second memory decay, 50 ms efficacy filter, gain 0.001 and efficacy bounds of 0.1–2× baseline are declared model choices. The anatomy-derived DAN-to-MBON contact fractions distribute modulation within each compartment. They are not measured dopamine concentrations or receptor kinetics.

The PAM11/MBON07 compartment is motivated by [Ichinose et al., 2015](https://elifesciences.org/articles/10719). Applying one rule to both α1 and γ1pedc compartments in this male reconstruction is **our unvalidated extension**, not a replication of either paper. Real fly dopamine can have context-dependent effects. “Profit dopamine” and “loss dopamine” are engineered assignments. Pain receptors, subjective pain, pleasure and consciousness are not modeled or measured.

## What would count as learning

The implementation can demonstrate that sensory input reaches memory cells, that selected dopamine cells spike, and that temporal pairing changes eligible synapses. Those are mechanism checks. Even when weights change, useful credit assignment through the fixed trade decoder is unproven.

To claim learned trading behavior requires held-out chronological market replay, independent starts, frozen-weight and shuffled-reinforcement controls, fees/slippage, equal budgets, retention, and loss of benefit after resetting learned weights. Compare to cash and simple exposure baselines as well: rising crypto prices alone can make any buyer look skilled. Avoid selecting a lucky run or tuning on the test period.

**No profitable learning, strategy improvement, biological replication, or live-funded performance has been demonstrated by this repository’s tests.** See [validation](validation.md) for the narrower checks actually performed.
