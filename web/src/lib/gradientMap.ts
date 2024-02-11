import {
  alldarkblue,
  alldarkgreen,
  alldarkred,
  blueGradient,
  greenGradient,
  redGradient,
} from "./gradients";

export const gradientMap = new Map();
gradientMap.set("red-tab", [redGradient, alldarkred]);
gradientMap.set("red-tab-icon", [redGradient, alldarkred]);
gradientMap.set("green-tab", [greenGradient, alldarkgreen]);
gradientMap.set("green-tab-icon", [greenGradient, alldarkgreen]);
gradientMap.set("blue-tab", [blueGradient, alldarkblue]);
gradientMap.set("blue-tab-icon", [blueGradient, alldarkblue]);
