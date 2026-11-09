# Spindle sound monitoring: idea and research notes

*Started 2026-11-09. Notes kept so this discussion can continue later.*

## The idea

I'm new to CNC and find it hard to know when feeds and cut depths go past what the machine can handle. The spindle and tool sound is a clear sign: a lower pitch means the spindle is slowing, which may mean a problem. The idea is to analyse the spindle and tool noise with a microphone.

## Does it already exist?

Yes, mostly in industry and research:

- **Chatter detection by microphone.** This is the most established use. *Harmonizer* from Manufacturing Laboratories listens to a cut, finds the chatter frequency and suggests a better spindle RPM. Some feeds-and-speeds apps have added similar phone-microphone features.
- **Tool condition monitoring.** Industrial systems from Caron, Artis/Marposs and Sandvik detect tool wear, breakage and overload. They usually use spindle power or current, vibration sensors, or ultrasonic acoustic-emission sensors rather than a normal microphone.
- **Research.** There are many papers on acoustic monitoring of milling, including ML classifiers for "good cut / chatter / dull tool / overload".

I don't know of a polished, cheap version aimed at hobby machines, so there is room for it.

## Viability

### Frequencies you can measure
- **Spindle rotation frequency** = RPM / 60. At 24,000 RPM that's 400 Hz; at 12,000 RPM it's 200 Hz. Any small imbalance makes a peak here.
- **Tooth-passing frequency** = RPM × flutes / 60. When the tool is cutting, this becomes the dominant tone. A 2-flute tool at 18,000 RPM gives 600 Hz.
- **Chatter** is a strong peak that is **not** a whole-number multiple of those frequencies. It sits near the machine's natural frequencies and is usually louder than the cutting tone.

### Detecting the speed drop is feasible
Hobby VFD induction spindles slip a few percent under heavy load. With a 1-second FFT window and peak interpolation you can resolve changes well under 0.5 Hz, about 0.1% at 400 Hz. That is easily enough.

### Caveats
1. **Electrical vs. mechanical noise.** The VFD and motor also produce a whine at the VFD output frequency, plus a carrier tone at a few kHz. That electrical tone does **not** drop under load; only the mechanical rotation slows. On a 2-pole spindle the two peaks are very close together, so the analysis must tell them apart.
2. **On hobby machines, spindle power is often not the first limit.** On routers and lighter mills, rigidity usually runs out first. That shows up as chatter, tool deflection or broken small tools rather than bogging down. Chatter detection may protect you more than slowdown detection, and the same FFT pipeline gives both.
3. **Background noise.** Dust extraction, shop vacuums and air blast are loud and broadband. A **contact sensor** helps: a piezo contact mic or an accelerometer clamped to the spindle mount picks up the machine and ignores the room.

### A simpler signal you may already have
If the VFD talks Modbus (PlanetCNC supports this), it can report output current or load directly. That is the most reliable overload signal and needs no audio. Best combination: current for overload, audio for chatter.

## Plan

1. **Idle sweep:** about 5 s at each RPM step (6k, 9k, 12k … 24k). For each step, find the 1× rotation peak, its harmonics and the VFD tone; check that the measured RPM matches the commanded RPM. This is the "healthy, unloaded" baseline.
2. **Air cut:** same RPMs with the axes moving, to see what the stepper/servo noise adds.
3. **Load ladder:** one material and one tool; a series of cuts with increasing depth or feed. Track rotation frequency, slip %, tooth-pass amplitude and any peaks that aren't harmonics. Note where the cut started to sound or look bad.
4. **Thresholds:** from that data, set limits such as "slip > X %" or "non-harmonic peak Y dB above the tooth-pass tone" and display them live.

## Implementation options

- **Python script (quick exploration):** record the idle sweep and plot the spectra. Best for step 1.
- **Webapp panel (final home):** the PlanetCNC webapp runs in a browser; the Web Audio API (`getUserMedia` + `AnalyserNode` or AudioWorklet) can do real-time FFT on a laptop mic or USB contact mic. The webapp knows the commanded RPM and the tool's flute count, so it can draw the expected 1×, tooth-pass and harmonic lines on a live spectrum. That is an advantage over generic chatter apps.

## Hardware choice (discussed 2026-11-09)

**Decision direction:** connect the sensor directly to the Raspberry Pi for continuous monitoring, using a **piezo contact sensor** rather than a microphone.

### Microphone vs. piezo

| | Microphone (USB / I2S MEMS) | Piezo contact sensor |
|---|---|---|
| What it hears | Everything in the room through the air | Vibration of whatever it's fixed to |
| Dust extraction / vacuum | Buries the spindle tone | Mostly ignored |
| Chatter | Detects it well | Detects it better (chatter is structural vibration) |
| 1× rotation peak | Weak, mixed with room echoes | Strong and clean near the spindle |
| Repeatability | Position-dependent | Very repeatable once rigidly fixed |
| Wiring to the Pi | Plug-and-play USB | Needs a high-impedance input |
| Cost | €5–30 | €2 disc + €20–50 interface |

### Connecting to the Pi
- The Pi has no audio input, so a USB audio interface is needed.
- **Impedance matters.** A piezo disc is about 20 nF. A normal ~2 kΩ mic input turns it into a filter that cuts everything below about **4 kHz**, exactly the range needed (200–2000 Hz). An input of 1 MΩ or more brings the cutoff down to a few Hz.
- **Option 1 (recommended):** a USB audio interface with a **Hi-Z / instrument (guitar) input**, e.g. Behringer UM2 class. Plug-and-play on the Pi, 48 kHz.
- **Option 2:** a JFET or op-amp piezo buffer preamp at the sensor, feeding the line input of a cheap USB sound card.

### Practical points
- **Interference from the VFD:** use shielded cable, keep it short, route it away from the spindle and motor cables, and put any preamp right at the sensor.
- **Mounting:** CA glue or epoxy, or a small aluminium holder bolted to the spindle mount or clamp. No foam tape (it absorbs high frequencies). Protect it from dust and coolant.
- **Heat:** 40–60 °C at the spindle housing is fine for piezo.
- **Pi load:** a continuous 48 kHz FFT is light for a Pi 3 or 4; a Pi Zero works with less headroom.
- **Optional:** a ~€10 USB mic for a side-by-side comparison during the idle sweep.

### Can an audio HAT replace the USB interface? (discussed 2026-11-09)
- **The Audiophonics I-Sabre LTE (ES9023) I already have will not work.** The ES9023 is a DAC only: its RCA jacks are line *outputs*, and the board has no ADC.
- An **ADC HAT** (e.g. HiFiBerry DAC+ ADC / ADC Pro) could work, but its line inputs are about 10–50 kΩ. With a 20 nF piezo that gives a cutoff of about 800 Hz (10 kΩ) or 170 Hz (47 kΩ), which loses the 1× rotation peak. A direct piezo can also clip a line input at resonance. It would need a **buffer** at the sensor: a TL072/OPA1642 non-inverting stage with a 1–10 MΩ input resistor, gain ≤1 or adjustable, and optional back-to-back clamp diodes. The only reason to choose it is stereo: a piezo and a mic recorded side by side.
- The webapp uses no GPIO, I2C or I2S, so a HAT would not conflict with it.
- **Use for the I-Sabre:** live monitoring. Play the piezo signal to headphones or a speaker during the idle sweep and load ladder, to hear where a cut starts to sound bad.
- **Conclusion:** keep the Hi-Z USB interface (plug-and-play). The cheaper alternative is a €5–10 USB sound card plus a piezo buffer.

### Shopping list
- [ ] 27–35 mm piezo discs (a pack)
- [ ] USB audio interface with a Hi-Z instrument input
- [ ] 1–2 m shielded mono cable + 6.3 mm jack
- [ ] CA glue / epoxy (or a small aluminium holder)
- [ ] (optional) USB microphone for comparison

## Status / next step

- [ ] Choose the first prototype: Python script or webapp panel
- [x] Choose hardware: piezo contact sensor + Hi-Z USB interface on the Pi (see above)
- [ ] Buy the hardware and mount the sensor
- [ ] Record the idle sweep (step 1)
- [ ] Check whether the VFD reports current over Modbus to PlanetCNC
