# GaiaLab

ESAC is responsible for developing and running AGIS, the software that computes the global astrometric parameters for the Gaia mission.
The design and validation of Gaia global astrometric mission requires to be able to run simulations that include complex calibration issues.
The current state of the art is AgisLab. This code is proprietary of DPAC, the scientific consortium processing the Gaia data
and responsible for the publication of the final star catalogue.

GaiaLab project is open source, developed by students and going some steps further in order to expose some of the global astrometric issues to a larger community.

The first version will be based on a very simple model :
* single source
* one ccd
* circular satellite orbit
* Newtonian physic (no relativity)

The project will make used of the technical notes written by Lennart Lindegren http://www.astro.lu.se/~lennart/Astrometry/TN.html

The codestyle tries to follow PEP8 guidelines, for example using linter 2.2 codestyle package. (see https://atom.io/packages/linter as of 20.09.2018) 
