#!/usr/bin/env node

const fs = require("fs");
const path = require("path");

const HEADER_SIZE = 21;
const DEFAULT_INPUT = path.resolve(__dirname, "raw");
const DEFAULT_OUTPUT = path.resolve(__dirname, "json");

function usage() {
  console.log(`Usage:
  node tools/sheep-maps/map-to-json.js [input] [output]

Arguments:
  input   .map file or directory. Defaults to tools/sheep-maps/raw
  output  .json file or directory. Defaults to tools/sheep-maps/json

Examples:
  node tools/sheep-maps/map-to-json.js
  node tools/sheep-maps/map-to-json.js tools/sheep-maps/raw/43dac4279ddd5b9cbdcebbde091b349d.map
  node tools/sheep-maps/map-to-json.js tools/sheep-maps/raw tools/sheep-maps/json
`);
}

function readVarint(buffer, state) {
  let result = 0;
  let shift = 0;

  while (state.offset < buffer.length) {
    const byte = buffer[state.offset++];
    result += (byte & 0x7f) * 2 ** shift;

    if ((byte & 0x80) === 0) return result;
    shift += 7;

    if (shift > 63) {
      throw new Error("Varint is too long");
    }
  }

  throw new Error("Unexpected end of file while reading varint");
}

function readLengthDelimited(buffer, state) {
  const length = readVarint(buffer, state);
  const start = state.offset;
  const end = start + length;

  if (end > buffer.length) {
    throw new Error("Length-delimited field exceeds buffer size");
  }

  state.offset = end;
  return buffer.subarray(start, end);
}

function skipField(buffer, state, wireType) {
  switch (wireType) {
    case 0:
      readVarint(buffer, state);
      return;
    case 1:
      state.offset += 8;
      return;
    case 2:
      readLengthDelimited(buffer, state);
      return;
    case 5:
      state.offset += 4;
      return;
    default:
      throw new Error(`Unsupported protobuf wire type: ${wireType}`);
  }
}

function parseMessage(buffer, handleField) {
  const state = { offset: 0 };

  while (state.offset < buffer.length) {
    const tag = readVarint(buffer, state);
    const fieldNumber = tag >> 3;
    const wireType = tag & 0x07;
    const handled = handleField(fieldNumber, wireType, buffer, state);

    if (!handled) {
      skipField(buffer, state, wireType);
    }
  }
}

function readString(buffer, state) {
  return readLengthDelimited(buffer, state).toString("utf8");
}

function readInt32(buffer, state) {
  return readVarint(buffer, state);
}

function parseBlockTypeDataEntry(buffer) {
  const entry = {};

  parseMessage(buffer, (fieldNumber, wireType, source, state) => {
    if (fieldNumber === 1 && wireType === 2) {
      entry.key = readString(source, state);
      return true;
    }
    if (fieldNumber === 2 && wireType === 0) {
      entry.value = readInt32(source, state);
      return true;
    }
    return false;
  });

  if (entry.key === undefined || entry.value === undefined) {
    throw new Error("Invalid blockTypeData map entry");
  }

  return entry;
}

function parseLevelDataEntry(buffer) {
  const entry = {};

  parseMessage(buffer, (fieldNumber, wireType, source, state) => {
    if (fieldNumber === 1 && wireType === 2) {
      entry.key = readString(source, state);
      return true;
    }
    if (fieldNumber === 2 && wireType === 2) {
      entry.value = parseNodeList(readLengthDelimited(source, state));
      return true;
    }
    return false;
  });

  if (entry.key === undefined || entry.value === undefined) {
    throw new Error("Invalid levelData map entry");
  }

  return entry;
}

function parseNodeList(buffer) {
  const nodes = [];

  parseMessage(buffer, (fieldNumber, wireType, source, state) => {
    if (fieldNumber === 1 && wireType === 2) {
      nodes.push(parseNode(readLengthDelimited(source, state)));
      return true;
    }
    return false;
  });

  return nodes;
}

function parseNode(buffer) {
  const node = {};

  parseMessage(buffer, (fieldNumber, wireType, source, state) => {
    if (fieldNumber === 1 && wireType === 2) {
      node.id = readString(source, state);
      return true;
    }
    if (fieldNumber === 2 && wireType === 0) {
      node.type = readInt32(source, state);
      return true;
    }
    if (fieldNumber === 3 && wireType === 0) {
      node.rolNum = readInt32(source, state);
      return true;
    }
    if (fieldNumber === 4 && wireType === 0) {
      node.rowNum = readInt32(source, state);
      return true;
    }
    if (fieldNumber === 5 && wireType === 0) {
      node.layerNum = readInt32(source, state);
      return true;
    }
    if (fieldNumber === 6 && wireType === 0) {
      node.moldType = readInt32(source, state);
      return true;
    }
    return false;
  });

  return node;
}

function parseGameMap(payload) {
  const gameMap = {
    blockTypeData: {},
    levelData: {},
  };

  parseMessage(payload, (fieldNumber, wireType, source, state) => {
    if (fieldNumber === 1 && wireType === 0) {
      gameMap.widthNum = readInt32(source, state);
      return true;
    }
    if (fieldNumber === 2 && wireType === 0) {
      gameMap.heightNum = readInt32(source, state);
      return true;
    }
    if (fieldNumber === 3 && wireType === 0) {
      gameMap.levelKey = readInt32(source, state);
      return true;
    }
    if (fieldNumber === 4 && wireType === 2) {
      const entry = parseBlockTypeDataEntry(readLengthDelimited(source, state));
      gameMap.blockTypeData[entry.key] = entry.value;
      return true;
    }
    if (fieldNumber === 5 && wireType === 2) {
      const entry = parseLevelDataEntry(readLengthDelimited(source, state));
      gameMap.levelData[entry.key] = entry.value;
      return true;
    }
    return false;
  });

  const layers = Object.keys(gameMap.levelData).sort((a, b) => Number(a) - Number(b));

  return {
    widthNum: gameMap.widthNum,
    heightNum: gameMap.heightNum,
    levelKey: gameMap.levelKey,
    blockTypeData: sortObjectByNumericKey(gameMap.blockTypeData),
    levelData: Object.fromEntries(layers.map((layer) => [layer, gameMap.levelData[layer]])),
    layers,
    operations: [],
  };
}

function sortObjectByNumericKey(value) {
  return Object.fromEntries(
    Object.keys(value)
      .sort((a, b) => Number(a) - Number(b))
      .map((key) => [key, value[key]]),
  );
}

function parseMapFile(inputFile) {
  const buffer = fs.readFileSync(inputFile);

  if (buffer.length <= HEADER_SIZE) {
    throw new Error(`File is too small to be a sheep .map file: ${inputFile}`);
  }

  const payload = buffer.subarray(HEADER_SIZE);
  return parseGameMap(payload);
}

function resolveJobs(inputPath, outputPath) {
  const inputStat = fs.statSync(inputPath);

  if (inputStat.isDirectory()) {
    const outputDir = outputPath;
    fs.mkdirSync(outputDir, { recursive: true });

    return fs.readdirSync(inputPath)
      .filter((file) => file.endsWith(".map"))
      .sort()
      .map((file) => ({
        inputFile: path.join(inputPath, file),
        outputFile: path.join(outputDir, file.replace(/\.map$/i, ".json")),
      }));
  }

  const outputStat = fs.existsSync(outputPath) ? fs.statSync(outputPath) : null;
  const outputFile = outputStat?.isDirectory()
    ? path.join(outputPath, path.basename(inputPath).replace(/\.map$/i, ".json"))
    : outputPath;

  fs.mkdirSync(path.dirname(outputFile), { recursive: true });
  return [{ inputFile: inputPath, outputFile }];
}

function summarize(mapJson) {
  const blockCount = Object.values(mapJson.levelData)
    .reduce((total, nodes) => total + nodes.length, 0);

  return {
    levelKey: mapJson.levelKey,
    layers: mapJson.layers.length,
    blocks: blockCount,
  };
}

function main() {
  const args = process.argv.slice(2);

  if (args.includes("-h") || args.includes("--help")) {
    usage();
    return;
  }

  const inputPath = path.resolve(args[0] ?? DEFAULT_INPUT);
  const outputPath = path.resolve(args[1] ?? DEFAULT_OUTPUT);
  const jobs = resolveJobs(inputPath, outputPath);

  if (jobs.length === 0) {
    throw new Error(`No .map files found in ${inputPath}`);
  }

  const summary = [];

  for (const job of jobs) {
    const mapJson = parseMapFile(job.inputFile);
    fs.writeFileSync(job.outputFile, `${JSON.stringify(mapJson, null, 2)}\n`);
    const info = summarize(mapJson);
    summary.push({ input: job.inputFile, output: job.outputFile, ...info });
    console.log(`converted ${path.relative(process.cwd(), job.inputFile)} -> ${path.relative(process.cwd(), job.outputFile)}`);
    console.log(`  levelKey=${info.levelKey} layers=${info.layers} blocks=${info.blocks}`);
  }

  if (jobs.length > 1) {
    const summaryFile = path.join(path.dirname(jobs[0].outputFile), "summary.json");
    fs.writeFileSync(summaryFile, `${JSON.stringify(summary, null, 2)}\n`);
    console.log(`summary ${path.relative(process.cwd(), summaryFile)}`);
  }
}

try {
  main();
} catch (error) {
  console.error(error instanceof Error ? error.message : error);
  process.exit(1);
}
