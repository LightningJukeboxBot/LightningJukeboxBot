import {
  blueDark,
  bluecolor,
  darkRed,
  greenDark,
  greencolor,
  superDark,
} from "../setup";
import { makeLinearGradient } from "../utils";

export const redGradient = makeLinearGradient(darkRed, superDark);
export const alldarkred = makeLinearGradient(superDark, superDark);
export const greenGradient = makeLinearGradient(greencolor, greenDark);
export const alldarkgreen = makeLinearGradient(greenDark, greenDark);
export const blueGradient = makeLinearGradient(bluecolor, blueDark);
export const alldarkblue = makeLinearGradient(blueDark, blueDark);
