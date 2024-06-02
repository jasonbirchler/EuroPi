from europi import *
import machine
from time import ticks_diff, ticks_ms
from random import uniform
from europi_script import EuroPiScript
from europi_config import EuroPiConfig
import gc
import math
import framebuf

"""
Egressus Melodiam (Stepped Melody)
author: Nik Ansell (github.com/gamecat69)
date: 19-Feb-24
labels: clocked lfo, sequencer, CV, randomness

Known Issues:
- When the clock rate or output division changes significantly this creates temporary wave shape discontinuities
- Input clocks > 150BPM can cause waveshape glitches. Workaround this by dividing the clock first if working at 150BPM or higher.
- Some input clocks < 16ms can be occasionally missed causing glitchy waves to he output or CV pattern steps to be missed.
Workaround this issue using longer clock pulse durations or 50% duty cycle square waves.
- Performance is affected when using the controls - the poor pico has trouble keeping up with the not-yet optimized code

Possible future work:

- Med [UX]: When in unclocked mode the pattern length cannot be changed - change UI? Or maybe restrict pattern length to 1 (LFO mode only in unclocked mode?)
- Low [FEATURE]: Find a use for ain? Change unClocked clock time?
- High [PERF]: See if performance can be optimized to work with clock inputs < 16ms

Programming notes:

Outputs -> CVPattern Banks -> CV Patterns data structure:

self.cvPatternBanks[idx][self.CvPattern][step]

cvPatternBank[idx] - Output: Each list item is a reference to an output
    [maxCvPatterns] - Cv Pattern Banks: Each list item is a reference to a CV Pattern
        [maxStepLength] - Cv Patterns: Each list item is a CV value per clock step

Slew / interpolation generator structures:

CV shapes are created using interpolation formulas that fill in the gaps between CV step values in a CV pattern.
Each interpolation formula is generated by an associated slew shape function.
There are both linear and non-linear interpolation functions that create various smooth and no-so-smooth shapes.

Each slew shape function generates an array of values (samples) between two given points.
Samples are output on the CV outputs based on the list of pre-computed interpolated values (sample buffers)
A value from the sample buffer is retrieved using a Python generator function (self.slewGenerator()).
One instance of the generator function for each each slew shape exists in a list: self.slewGeneratorObjects[]

A new sample buffer (array of interpolated values) is created at each clock step.
The number of samples required in each sample buffer (one for each output) is calculated at each clock step or if
the clock rate or output division changes.

The timing and associated hanlding of input clock interrupts (clocks to din) is not perfect - python/micropython is not
known to be great at sub-second timing!
Therefore, at the end of each clock step the algorithm tunes itself to work around sample buffer overruns or underruns.
If there is a buffer underrun (not enough samples in the buffer), the previous output voltage (sample) is used until the algorithm
catches back up with itself.

slewGeneratorObjects[] contains 6 object (one for each output)
Each slewGeneratorObjects[] object is a reference to a copy of the self.slewGenerator() function.
The self.slewGenerator() function receives a buffer filled with samples and yields one sample each time it is called using next()

In order to maintain the best balance of smooth waves and Rpi pico memory usage, an algorithm is used to vary the
sample rate automatically based on the selected output division.
This causes the sample rate to be at minium when there is a slow clock and high output division.
Conversely, when there is a fast clock and low output division a higher sample rate is used to avoid unwanted steps
often caused by under-samping a wave form.

"""
# Minimum allowed time between incoming 16th note clocks. Smaller values are too fast for poor ole micropython to keep up
MIN_CLOCK_TIME_MS = 50

# Maximum allowed time between incoming 16th note clocks. This is 2 BPM, larger value causes memory issues unless the next two vals are halfed
MAX_CLOCK_TIME_MS = 3750

# Max sample rate and clock divisor to keep required buffer sizes within memory limits
MAX_SAMPLE_RATE = 32
MAX_OUTPUT_DENOMINATOR = 8

MIN_MS_BETWEEN_SAVES = 2000

# Calculate maximum sample buffer size required
SLEW_BUFFER_SIZE_IN_SAMPLES = int(
    (MAX_CLOCK_TIME_MS / 1000) * MAX_SAMPLE_RATE * MAX_OUTPUT_DENOMINATOR
)

# Reduce knob hysteresis using this value - Mutable Instruments style
KNOB_CHANGE_TOLERANCE = 0.999

# Set the maximum CV voltage using a global config value
# Important: Needs firmware v0.12.1 or higher
MAX_CV_VOLTAGE = europi_config.MAX_OUTPUT_VOLTAGE

MAX_STEP_LENGTH = 32

# Diff between incoming clocks are stored in the FiFo buffer and averaged
# Averaging over 5 values seems to deal with wonky clocks quite well
CLOCK_DIFF_BUFFER_LEN = 5

# If the clock rate changes more than this, trigger a recalculation.
# Avoids wonky waves when wonky clocks are used.
MIN_CLOCK_CHANGE_DETECTION_MS = 100

# Slightly quicker way to get integers from boolean values
BOOL_DICT = {False: 0, True: 1}

# Wave shape bit arrays
WAVE_SHAPE_IMGS = [
    bytearray(
        b"\xfe\x10\x82\x10\x82\x10\x82\x10\x82\x10\x82\x10\x82\x10\x82\x10\x82\x10\x82\x10\x82\x10\x83\xf0"
    ),  # stepUpStepDown
    bytearray(
        b"\x00\x00\x06\x00\x05\x00\t\x00\t\x00\x10\x80\x10\x80 @ @@ @ \x80\x10"
    ),  # linspace (tri)
    bytearray(b"0\x00(\x10D\x10D\x10D D\x10\x82 \x82 \x82 \x82 \x81@\x01\xc0"),  # smooth (sine)
    bytearray(
        b"\x04\x00\x04\x00\x06\x00\x06\x00\n\x00\t\x00\t\x00\x10\x80 \x80 @@ \x80\x10"
    ),  # expUpexpDown
    bytearray(
        b'\x0c\x00\x12\x00\x12\x00"\x00"\x00A\x00A\x00@\x80@\x80\x80@\x80 \x80\x10'
    ),  # sharkTooth
    bytearray(
        b"\x04\x00\x05\x00\x04\x80\x08\x80\x08@\x08@\x10 \x10 \x10   @\x10\x80\x00"
    ),  # sharkToothReverse
    bytearray(
        b"\x03\xf0\x0c\x100\x10 \x10@\x10@\x10@\x10\x80\x10\x80\x10\x80\x10\x80\x10\x80\x10"
    ),  # logUpStepDown
    bytearray(
        b"\xff\x80\x80\x80\x80\x80\x80\x80\x80\x80\x80\x80\x80\x80\x80\x80\x80@\x80@\x80 \x80\x10"
    ),  # stepUpExpDown
]


class EgressusMelodiam(EuroPiScript):
    def __init__(self):

        # Initialize variables
        self.newClockToProcess = False
        self.clockStep = 0
        self.stepPerOutput = [0, 0, 0, 0, 0, 0]
        self.nextStepPerOutput = [0, 0, 0, 0, 0, 0]
        self.CvPattern = 0
        self.resetTimeout = MAX_CLOCK_TIME_MS
        self.screenRefreshNeeded = True
        self.showNewPatternIndicator = False
        self.showNewPatternIndicatorClockStep = 0

        self.numCvPatterns = 1  # Leave at 1 due to memory limitations
        self.maxCvPatterns = 1  # Leave at 1 due to memory limitations

        self.slewArray = []
        self.lastClockTime = 0
        self.lastSlewVoltageOutputTime = [0, 0, 0, 0, 0, 0]
        self.slewGeneratorObjects = [
            self.slewGenerator([0]),
            self.slewGenerator([0]),
            self.slewGenerator([0]),
            self.slewGenerator([0]),
            self.slewGenerator([0]),
            self.slewGenerator([0]),
        ]
        self.slewShapes = [
            self.stepUpStepDown,
            self.linspace,
            self.smooth,
            self.expUpexpDown,
            self.sharkTooth,
            self.sharkToothReverse,
            self.logUpStepDown,
            self.stepUpExpDown,
        ]
        self.voltageExtremes = [0, MAX_CV_VOLTAGE]
        self.outputVoltageFlipFlops = [
            True,
            True,
            True,
            True,
            True,
            True,
        ]  # Flipflops between self.VoltageExtremes for LFO mode

        self.selectedOutput = 0
        self.lastK1Reading = 0
        self.currentK1Reading = 0
        self.lastK2Reading = 0
        self.currentK2Reading = 0

        self.running = False
        self.bufferUnderrunCounter = [0, 0, 0, 0, 0, 0]
        self.bufferOverrunSamples = [0, 0, 0, 0, 0, 0]
        self.samplesPerSec = [0, 0, 0, 0, 0, 0]
        self.msBetweenSamples = [0, 0, 0, 0, 0, 0]

        self.unClockedMode = False
        self.lastClockTime = ticks_ms()
        self.lastSaveState = ticks_ms()
        self.pendingSaveState = False
        self.previousOutputVoltage = [0, 0, 0, 0, 0, 0]
        self.slewBufferSampleNum = [0, 0, 0, 0, 0, 0]
        self.slewBufferPosition = [0, 0, 0, 0, 0, 0]
        self.bufferSampleOffsets = [0, 0, 0, 0, 0, 0]
        self.squareOutputs = [0, 0, 0, 0, 0, 0]

        self.loadState()
        # pre-create slew buffers to avoid memory allocation errors
        self.initSlewBuffers()

        # Initialize inputClockDiffs  using previous self.msBetweenClocks from loadState()
        self.inputClockDiffs = []

        # Init clock diff buffer with the default or saved value
        for n in range(CLOCK_DIFF_BUFFER_LEN):
            self.inputClockDiffs.append(self.msBetweenClocks)
        self.averageMsBetweenClocks = self.average(self.inputClockDiffs)

        # Clock rate or output division changed, recalculate optimal sample rate
        self.calculateOptimalSampleRate()

        # -----------------------------
        # Interupt Handling functions
        # -----------------------------

        @din.handler
        def clockTrigger():
            """Triggered on each rising edge into digital input. Sets running flag to true.
            Sets a flag to tell main() to process the clock step. Ignored in unclocked mode."""

            self.running = True

            if not self.unClockedMode:
                self.newClockToProcess = True

        @b1.handler_falling
        def b1Pressed():
            """Triggered when B1 is pressed and released"""
            if (
                ticks_diff(ticks_ms(), b1.last_pressed()) > 2000
                and ticks_diff(ticks_ms(), b1.last_pressed()) < 5000
            ):
                # long press generate new CV pattern
                self.generateNewRandomCVPattern(new=False, activePatternOnly=True)
                self.showNewPatternIndicator = True
                self.screenRefreshNeeded = True
                self.showNewPatternIndicatorClockStep = self.clockStep
                self.pendingSaveState = True
                #self.saveState()
            else:
                # short press change slew mode
                self.outputSlewModes[self.selectedOutput] = (
                    self.outputSlewModes[self.selectedOutput] + 1
                ) % len(self.slewShapes)
                self.screenRefreshNeeded = True
                self.pendingSaveState = True
                #self.saveState()

        @b2.handler_falling
        def b2Pressed():
            """Triggered when B2 is pressed and released"""
            if (
                ticks_diff(ticks_ms(), b2.last_pressed()) > 2000
                and ticks_diff(ticks_ms(), b2.last_pressed()) < 5000
            ):
                # long press change to unclocked mode
                self.unClockedMode = not self.unClockedMode
                if self.unClockedMode:
                    self.running = True
                self.pendingSaveState = True
                #self.saveState()

                # Update previous knob values to avoid them changing when the mode changes
                self.lastK1Reading = self.currentK1Reading
                self.lastK2Reading = self.currentK2Reading
                self.screenRefreshNeeded = True

            else:
                # short press change selected output
                self.selectedOutput = (self.selectedOutput + 1) % 6
                self.screenRefreshNeeded = True
                self.pendingSaveState = True
                #self.saveState()

    def calculateOptimalSampleRate(self):
        """Calculate optimal sample rate for smooth CV output while using minimal memory"""
        for idx in range(len(cvs)):
            self.samplesPerSec[idx] = int(
                min(2 *
                    (MAX_SAMPLE_RATE / self.outputDivisions[idx])
                    * (MAX_CLOCK_TIME_MS / self.averageMsBetweenClocks),
                    MAX_SAMPLE_RATE,
                )
            )
            self.msBetweenSamples[idx] = int(1000 / self.samplesPerSec[idx])

    def initSlewBuffers(self):
        """Create slew buffers and fill with zeros"""
        self.slewBuffers = []
        for n in range(6):  # for each output 0-5
            self.slewBuffers.append([])  # add new empty list to the buffer list
            for m in range(SLEW_BUFFER_SIZE_IN_SAMPLES):
                self.slewBuffers[n].append(0)

    def average(self, list):
        """Pythonic mean average function"""
        myList = list.copy()
        return sum(myList) / len(myList)

    def initCvPatternBanks(self):
        """Initialize CV pattern banks"""
        # Init CV pattern banks, one for each output
        self.cvPatternBanks = [[], [], [], [], [], []]
        for n in range(self.maxCvPatterns):
            self.generateNewRandomCVPattern(self)
        return self.cvPatternBanks

    def generateNewRandomCVPattern(self, new=True, activePatternOnly=False):
        """Generate new CV pattern for existing bank or create a new bank

        @param new  If true, create a new pattern/overwrite the existing one. Otherwise re-use the existing pattern
        @param activePatternOnly  If true, generate pattern for selected output. Otherwise generate pattern for all outputs

        @return True if the pattern(s) were successfully generated, otherwise False
        """
        # Note: This function is capable of working with multiple pattern banks
        #  However, due to current memory limitations only one pattern bank is used
        try:
            gc.collect()
            if new:
                # new flag provided, create new list
                if activePatternOnly:
                    self.cvPatternBanks[self.selectedOutput].append(
                        self.generateRandomPattern(MAX_STEP_LENGTH, 0, MAX_CV_VOLTAGE)
                    )
                else:
                    for pattern in self.cvPatternBanks:
                        pattern.append(
                            self.generateRandomPattern(MAX_STEP_LENGTH, 0, MAX_CV_VOLTAGE)
                        )
            else:
                # Update existing list
                if activePatternOnly:
                    self.cvPatternBanks[self.selectedOutput][self.CvPattern] = (
                        self.generateRandomPattern(MAX_STEP_LENGTH, 0, MAX_CV_VOLTAGE)
                    )
                else:
                    for pattern in self.cvPatternBanks:
                        pattern[self.CvPattern] = self.generateRandomPattern(
                            MAX_STEP_LENGTH, 0, MAX_CV_VOLTAGE
                        )
            return True
        except Exception:
            return False

    def generateRandomPattern(self, length, min, max):
        """Generate a random pattern of a desired length containing values between min and max

        @param length  The length of the desired pattern
        @param min  The minimum value for pattern elements (inclusive)
        @param max  The maximum value for pattern elements (inclusive)

        @return  The generated pattern
        """
        self.t = []
        for i in range(0, length):
            self.t.append(round(uniform(min, max), 3))
        return self.t

    def main(self):
        """Entry point - main loop. See inline comments for more info"""
        while True:
            self.updateScreen()
            self.getK1Value()
            self.getOutputDivision()

            if self.newClockToProcess:

                # Get the time difference since the last clockTime
                newDiffBetweenClocks = min(MAX_CLOCK_TIME_MS, ticks_ms() - self.lastClockTime)
                self.lastClockTime = ticks_ms()

                # Add time diff between clocks to inputClockDiffs Fifo list, skipping the first clock as we have no reference
                if self.clockStep > 0:
                    self.inputClockDiffs[self.clockStep % CLOCK_DIFF_BUFFER_LEN] = (
                        newDiffBetweenClocks
                    )

                # Clock rate change detection
                if (
                    self.clockStep >= CLOCK_DIFF_BUFFER_LEN
                    and abs(newDiffBetweenClocks - self.averageMsBetweenClocks)
                    > MIN_CLOCK_CHANGE_DETECTION_MS
                ):
                    # Update average ms between clocks
                    self.averageMsBetweenClocks = self.average(self.inputClockDiffs)
                    # Clock rate or output division changed, recalculate optimal sample rate
                    self.calculateOptimalSampleRate()

                self.handleClockStep()
                # Incremenent the clock step
                self.clockStep += 1
                self.newClockToProcess = False

            # Cycle through outputs, process when needed
            for idx in range(len(cvs)):
                if (
                    ticks_diff(ticks_ms(), self.lastSlewVoltageOutputTime[idx])
                    >= self.msBetweenSamples[idx]
                    and self.running
                ):

                    try:

                        # Do we have a sample in the buffer?
                        if self.slewBufferPosition[idx] < self.slewBufferSampleNum[idx]:
                            # Yes, we have a sample, output voltage to match the sample, reset underrun counter and advance position in buffer

                            # If a square interpolation mode (0) a precalculated value is used
                            # as the generator function for square waves was really buggy
                            if self.outputSlewModes[idx] == 0:
                                v = self.squareOutputs[idx]
                            else:
                                v = next(self.slewGeneratorObjects[idx])
                            cvs[idx].voltage(v)
                            self.previousOutputVoltage[idx] = v
                            self.bufferUnderrunCounter[idx] = 0
                        else:
                            # We do not have a sample - buffer under run
                            # Output the previous voltage to keep things as smooth as possible
                            cvs[idx].voltage(self.previousOutputVoltage[idx])
                            self.bufferUnderrunCounter[idx] += 1

                        # Advance the position in the sample/slew buffer
                        self.slewBufferPosition[idx] += 1

                        # Update the last sample output time
                        self.lastSlewVoltageOutputTime[idx] = ticks_ms()

                    except StopIteration:
                        continue

            # Save state
            if self.pendingSaveState and ticks_diff(ticks_ms(), self.lastSaveState) >= MIN_MS_BETWEEN_SAVES:
                self.saveState()
                self.pendingSaveState = False

            # If we are not being clocked, trigger a clock after the configured clock time
            if (
                self.unClockedMode
                and ticks_diff(ticks_ms(), self.lastClockTime) >= self.averageMsBetweenClocks
            ):
                self.running = True
                self.lastClockTime = ticks_ms()
                self.handleClockStep()
                self.clockStep += 1

            # If I have been running, then stopped for longer than resetTimeout, reset all steps to 0
            if (
                not self.unClockedMode
                and self.clockStep != 0
                and ticks_diff(ticks_ms(), din.last_triggered()) > self.resetTimeout
            ):
                for idx in range(len(cvs)):
                    self.stepPerOutput[idx] = 0
                # Update screen with the upcoming CV pattern
                self.screenRefreshNeeded = True
                self.pendingSaveState = True
                #self.saveState()
                self.running = False
                for cv in cvs:
                    cv.off()

                self.bufferUnderrunCounter = [0, 0, 0, 0, 0, 0]
                self.bufferOverrunSamples = [0, 0, 0, 0, 0, 0]
                self.slewBufferPosition = [0, 0, 0, 0, 0, 0]
                self.bufferSampleOffsets = [0, 0, 0, 0, 0, 0]

    def handleClockStep(self):
        """Advances step and generates new slew voltages to next value in CV pattern"""

        # Cycle through outputs and generate slew for each
        for idx in range(len(cvs)):

            # If the clockstep is a division of the output division
            if self.clockStep % (self.outputDivisions[idx]) == 0:
                # flip the flip flop value for LFO mode

                self.outputVoltageFlipFlops[idx] = not self.outputVoltageFlipFlops[idx]

                # Catch buffer over-runs by detecting that not all samples were used in the last cycle
                if self.clockStep > CLOCK_DIFF_BUFFER_LEN and not self.unClockedMode:
                    self.bufferOverrunSamples[idx] = int(
                        self.slewBufferPosition[idx] - self.slewBufferSampleNum[idx]
                    )

                    self.bufferSampleOffsets[idx] = (
                        self.bufferSampleOffsets[idx] - self.bufferOverrunSamples[idx]
                    )

                else:
                    self.bufferSampleOffsets[idx] = 0

                # Set the target number of samples for the next cycle, factoring in any previous overruns
                # Calculate the number of samples needed until the next clock
                self.slewBufferSampleNum[idx] = min(
                    SLEW_BUFFER_SIZE_IN_SAMPLES,
                    int(
                        (
                            (self.averageMsBetweenClocks / 1000)
                            * self.outputDivisions[idx]
                            * self.samplesPerSec[idx]
                        )
                        - self.bufferSampleOffsets[idx]
                    ),
                )

                # If length is one, cycle between high and low voltages (traditional LFO)
                # Each output uses a its configured slew shape
                if self.patternLength == 1:

                    # If square transition, set next output value to be one of the voltage extremes (flipping each time)
                    if self.outputSlewModes[idx] == 0:
                        self.squareOutputs[idx] = self.voltageExtremes[
                            BOOL_DICT[self.outputVoltageFlipFlops[idx]]
                        ]
                    else:
                        self.slewArray = self.slewShapes[self.outputSlewModes[idx]](
                            self.voltageExtremes[BOOL_DICT[self.outputVoltageFlipFlops[idx]]],
                            self.voltageExtremes[BOOL_DICT[not self.outputVoltageFlipFlops[idx]]],
                            self.slewBufferSampleNum[idx],
                            self.slewBuffers[idx],
                        )
                else:
                    # If square transition, just output the CV value in the pattern associated with the current step
                    if self.outputSlewModes[idx] == 0:
                        self.squareOutputs[idx] = self.cvPatternBanks[idx][self.CvPattern][
                            self.stepPerOutput[idx]
                        ]
                    else:
                        self.slewArray = self.slewShapes[self.outputSlewModes[idx]](
                            self.cvPatternBanks[idx][self.CvPattern][self.stepPerOutput[idx]],
                            self.cvPatternBanks[idx][self.CvPattern][self.nextStepPerOutput[idx]],
                            self.slewBufferSampleNum[idx],
                            self.slewBuffers[idx],
                        )

                # Update the function object reference to the generator function, passing it the latest slewArray sample buffer
                self.slewGeneratorObjects[idx] = self.slewGenerator(self.slewArray)

                # Go back to the start of the buffer
                self.slewBufferPosition[idx] = 0

                # Calculate next steps (indexs in CV patterns)
                self.stepPerOutput[idx] = ((self.stepPerOutput[idx] + 1)) % self.patternLength
                self.nextStepPerOutput[idx] = ((self.stepPerOutput[idx] + 1)) % self.patternLength

        # Hide the shreaded visual indicator after 2 clock steps
        if self.clockStep > self.showNewPatternIndicatorClockStep + 2:
            self.showNewPatternIndicator = False

    def getK1Value(self):
        """Get the k1 value, update params if changed"""

        self.currentK1Reading = k1.read_position(100) + 1

        if abs(self.currentK1Reading - self.lastK1Reading) > KNOB_CHANGE_TOLERANCE:
            # knob has moved
            if self.unClockedMode:
                # Set clock speed based on k1 value. This calc creates knob increments of 75ms
                self.averageMsBetweenClocks = (
                    self.currentK1Reading * (MAX_CLOCK_TIME_MS / MIN_CLOCK_TIME_MS) / 2
                )

                # clock rate or output division changed, calculate optimal sample rate
                self.calculateOptimalSampleRate()

            else:
                # Set pattern length
                self.patternLength = int((MAX_STEP_LENGTH / 100) * (self.currentK1Reading - 1)) + 1

            # Something changed, update screen and save state
            self.pendingSaveState = True
            #self.saveState()
            self.screenRefreshNeeded = True

        self.lastK1Reading = self.currentK1Reading

    def getOutputDivision(self):
        """Get the output division from k2"""
        self.currentK2Reading = k2.read_position(MAX_OUTPUT_DENOMINATOR) + 1

        if self.currentK2Reading != self.lastK2Reading:
            self.outputDivisions[self.selectedOutput] = k2.read_position(MAX_OUTPUT_DENOMINATOR) + 1
            self.screenRefreshNeeded = True
            self.lastK2Reading = self.currentK2Reading
            # clock rate or output division changed, calculate optimal sample rate
            self.calculateOptimalSampleRate()
            self.pendingSaveState = True
            #self.saveState()

    def saveState(self):
        """Save working vars to a save state file"""
        self.state = {
            "cvPatternBanks": self.cvPatternBanks,
            "CvPattern": self.CvPattern,
            "outputSlewModes": self.outputSlewModes,
            "outputDivisions": self.outputDivisions,
            "patternLength": self.patternLength,
            "msBetweenClocks": self.msBetweenClocks,
            "unClockedMode": self.unClockedMode,
        }
        self.save_state_json(self.state)
        self.lastSaveState = ticks_ms()

    def loadState(self):
        """Load a previously saved state, or initialize working vars, then save"""
        self.state = self.load_state_json()
        self.cvPatternBanks = self.state.get("cvPatternBanks", [])
        self.CvPattern = self.state.get("CvPattern", 0)
        self.outputSlewModes = self.state.get("outputSlewModes", [0, 0, 0, 0, 0, 0])
        self.outputDivisions = self.state.get("outputDivisions", [1, 2, 4, 1, 2, 4])
        self.patternLength = self.state.get("patternLength", 8)
        self.msBetweenClocks = self.state.get("msBetweenClocks", 976)
        self.unClockedMode = self.state.get("unClockedMode", False)

        if len(self.cvPatternBanks) == 0:
            self.initCvPatternBanks()

        self.pendingSaveState = True
        #self.saveState()
        # Let the rest of the script know how many pattern banks we have
        self.numCvPatterns = len(self.cvPatternBanks[0])

    def drawWave(self):
        """UI wave visualizations"""
        fb = framebuf.FrameBuffer(
            WAVE_SHAPE_IMGS[self.outputSlewModes[self.selectedOutput]], 12, 12, framebuf.MONO_HLSB
        )
        oled.blit(fb, 0, 20)

    def updateScreen(self):
        """Update the screen only if something has changed. oled.show() hogs the processor and causes latency."""

        # Only update if something has changed
        if not self.screenRefreshNeeded:
            return
        # Clear screen
        oled.fill(0)

        # Selected output
        oled.fill_rect(108, 0, 20, 9, 1)
        oled.text(f"{self.selectedOutput + 1}", 115, 1, 0)

        # Show division for selected output
        number = self.outputDivisions[self.selectedOutput]
        x = 111 if number >= 10 else 115
        oled.text(f"{number}", x, 12, 1)

        # Show wave for selected output
        self.drawWave()

        if self.unClockedMode:

            if self.averageMsBetweenClocks != 0:
                oled.text(f"{int(self.averageMsBetweenClocks * 2)} ms", 31, 1, 1)

        else:

            # Draw pattern length
            row1 = ""
            row2 = ""
            row3 = ""
            row4 = ""
            if self.patternLength > 24:
                # draw two rows
                row1 = "........"
                row2 = "........"
                row3 = "........"
                for i in range(24, self.patternLength):
                    row4 = row4 + "."
            elif self.patternLength > 16:
                row1 = "........"
                row2 = "........"
                for i in range(16, self.patternLength):
                    row3 = row3 + "."
            elif self.patternLength > 8:
                row1 = "........"
                for i in range(8, self.patternLength):
                    row2 = row2 + "."
            else:
                # draw one row
                for i in range(self.patternLength):
                    row1 = row1 + "."

            xStart = 27
            oled.text(row1, xStart, 0, 1)
            oled.text(row2, xStart, 6, 1)
            oled.text(row3, xStart, 12, 1)
            oled.text(row4, xStart, 18, 1)

        # Draw a visual cue for when a long button press has been detected
        # and a new random pattern is being generated
        if self.showNewPatternIndicator:
            fb = framebuf.FrameBuffer(
                bytearray(b"\x0f\x000\x80N`Q \x94\xa0\xaa\x90\xa9P\xa5@Z\x80H\x803\x00\x0c\x00"),
                12,
                12,
                framebuf.MONO_HLSB,
            )
            oled.blit(fb, 0, 0)

        oled.show()

    # -----------------------------
    # Slew functions
    # -----------------------------

    def stepUpStepDown(self, start, stop, num, buffer):
        """Produces step up, step down

        @param start  Starting value
        @param stop   Target value
        @param num    Number of samples required
        @param buffer Pointer to fill with samples

        @return  The edited buffer
        """
        c = 0
        if self.patternLength == 1:  # LFO Mode, make sure we complete a full cycle
            for i in range(num / 2):
                buffer[c] = start
                c += 1
            for i in range(num / 2):
                buffer[c] = stop
                c += 1
        else:
            for i in range(num - 1):
                buffer[c] = stop
                c += 1
        return buffer

    def linspace(self, start, stop, num, buffer):
        """Produces a linear transition

        @param start  Starting value
        @param stop   Target value
        @param num    Number of samples required
        @param buffer Pointer to fill with samples

        @return  The edited buffer
        """
        c = 0
        num = max(1, num)  # avoid divide by zero
        diff = (float(stop) - start) / (num)
        for i in range(num):
            val = (diff * i) + start
            buffer[c] = val
            c += 1
        return buffer

    def logUpStepDown(self, start, stop, num, buffer):
        """Produces a log up/step down transition

        @param start  Starting value
        @param stop   Target value
        @param num    Number of samples required
        @param buffer Pointer to fill with samples

        @return  The edited buffer
        """
        c = 0
        if self.patternLength == 1:  # LFO Mode, make sure we complete a full cycle
            for i in range(num / 2):
                i = max(i, 1)
                x = 1 - ((stop - float(start)) / (i)) + (stop - 1)
                buffer[c] = x
                c += 1
            for i in range(num / 2):
                buffer[c] = stop
                c += 1
        else:
            if stop >= start:
                for i in range(num):
                    i = max(i, 1)
                    x = 1 - ((stop - float(start)) / (i)) + (stop - 1)
                    buffer[c] = x
                    c += 1
            else:
                for i in range(num):
                    buffer[c] = stop
                    c += 1
        return buffer

    def stepUpExpDown(self, start, stop, num, buffer):
        """Produces a step up, exponential down transition

        @param start  Starting value
        @param stop   Target value
        @param num    Number of samples required
        @param buffer Pointer to fill with samples

        @return  The edited buffer
        """
        c = 0
        if stop <= start:
            for i in range(num):
                i = max(i, 1)
                x = 1 - ((stop - float(start)) / (i)) + (stop - 1)
                buffer[c] = x
                c += 1
        else:
            for i in range(num):
                buffer[c] = stop
                c += 1
        return buffer

    def smooth(self, start, stop, num, buffer):
        """Produces smooth curve using half a cosine wave

        @param start  Starting value
        @param stop   Target value
        @param num    The number of samples required
        @param buffer Pointer to fill with samples

        @return  The edited buffer
        """
        c = 0
        freqHz = 0.5  # We want to complete half a cycle
        amplitude = abs(
            (stop - start) / 2
        )  # amplitude is half of the diff between start and stop (this is peak to peak)
        if start <= stop:
            # Starting position is 90 degrees (cos) at 'start' volts
            startOffset = num
            amplitudeOffset = start
        else:
            # Starting position is 0 degrees (cos) at 'stop' volts
            startOffset = 0
            amplitudeOffset = stop
        for i in range(num):
            i += startOffset
            val = amplitude + float(
                amplitude * math.cos(2 * math.pi * freqHz * i / float(num))
            )
            buffer[c] = round(val + amplitudeOffset, 4)
            c += 1
        return buffer

    def expUpexpDown(self, start, stop, num, buffer):
        """Produces pointy exponential wave using a quarter cosine up and a quarter cosine down

        @param start  Starting value
        @param stop   Target value
        @param num    The number of samples required
        @param buffer Pointer to fill with samples

        @return  The edited buffer
        """
        c = 0
        freqHz = 0.25  # We want to complete quarter of a cycle
        amplitude = abs(
            (stop - start)
        )  # amplitude is half of the diff between start and stop (this is peak to peak)
        if start <= stop:
            startOffset = num * 2
            amplitudeOffset = start
            for i in range(num):
                i += startOffset
                val = amplitude + float(
                    amplitude * math.cos(2 * math.pi * freqHz * i / float(num))
                )
                buffer[c] = round(val + amplitudeOffset, 4)
                c += 1
        else:
            startOffset = num
            amplitudeOffset = stop
            for i in range(num):
                i += startOffset
                val = amplitude + float(
                    amplitude * math.cos(2 * math.pi * freqHz * i / float(num))
                )
                buffer[c] = round(val + amplitudeOffset, 4)
                c += 1
        return buffer

    def sharkTooth(self, start, stop, num, buffer):
        """Produces a sharktooth wave with an approximate log curve up and approximate
        exponential curve down

        @param start  Starting value
        @param stop   Target value
        @param num    The number of samples required
        @param buffer Pointer to fill with samples

        @return  The edited buffer
        """
        c = 0
        freqHz = 0.25  # We want to complete quarter of a cycle
        amplitude = abs(
            (stop - start)
        )  # amplitude is half of the diff between start and stop (this is peak to peak)
        if start <= stop:
            startOffset = num * 3
            amplitudeOffset = start - amplitude
            for i in range(num):
                i += startOffset
                val = amplitude + float(
                    amplitude * math.cos(2 * math.pi * freqHz * i / float(num))
                )
                buffer[c] = round(val + amplitudeOffset, 4)
                c += 1
        else:
            startOffset = num
            amplitudeOffset = stop
            for i in range(num):
                i += startOffset
                val = amplitude + float(
                    amplitude * math.cos(2 * math.pi * freqHz * i / float(num))
                )
                buffer[c] = round(val + amplitudeOffset, 4)
                c += 1
        return buffer

    def sharkToothReverse(self, start, stop, num, buffer):
        """Produces a reverse sharktooth wave with an approximate exponential curve up and approximate
        log curve down

        @param start  Starting value
        @param stop   Target value
        @param num    The number of samples required
        @param buffer Pointer to fill with samples

        @return  The edited buffer
        """
        c = 0
        freqHz = 0.25  # We want to complete quarter of a cycle
        amplitude = abs(
            (stop - start)
        )  # amplitude is half of the diff between start and stop (this is peak to peak)
        if start <= stop:
            startOffset = num * 2
            amplitudeOffset = start
            for i in range(num):
                i += startOffset
                val = amplitude + float(
                    amplitude * math.cos(2 * math.pi * freqHz * i / float(num))
                )
                buffer[c] = round(val + amplitudeOffset, 4)
                c += 1
        else:
            startOffset = 0
            amplitudeOffset = 1 - (amplitude - stop + 1)
            for i in range(num):
                i += startOffset
                val = amplitude + float(
                    amplitude * math.cos(2 * math.pi * freqHz * i / float(num))
                )
                buffer[c] = round(val + amplitudeOffset, 4)
                c += 1
        return buffer

    def slewGenerator(self, arr):
        """Generator function that returns the next slew sample from the specified list

        @param  The list of samples to choose from
        """
        for s in range(len(arr)):
            yield arr[s]


if __name__ == "__main__":
    dm = EgressusMelodiam()
    dm.main()