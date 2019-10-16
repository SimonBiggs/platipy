#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Tues Aug 13

"""
import os, re, sys, datetime, warnings

from functools import reduce

import numpy as np
import SimpleITK as sitk
import vtk

from scipy.stats import norm as scipy_norm
from scipy.optimize import curve_fit
from scipy.ndimage import filters
from scipy.ndimage import measurements
from scipy.interpolate import griddata
from scipy.interpolate import RectSphereBivariateSpline

from vtk.util.numpy_support import vtk_to_numpy, numpy_to_vtk

debug=True

def ThresholdAndMeasureLungVolume(image, l=0, u=1):
    """
    Thresholds an image using upper and lower bounds in the intensity.
    Each non-connected component has the volume and perimeter/border ratio measured

    Args
        image (sitk.Image)  : the input image
        l (float)           : the lower threshold
        u (float)           : the upper threshold

    Returns
        NP (np.ndarray)     : a one-dimensional array of the number of pixels in each component
        PBR (np.ndarray)    : a one-dimensional array of the perimeter/border ratio for each component
        mask (sitk.Image)   : the connected component label map
        maxVals (np.ndarray): a one-dimensional array of the label map values

    """
    # Perform the threshold
    imThresh = sitk.Threshold(image, lower=l, upper=u)

    # Create a connected component image
    mask = sitk.ConnectedComponent(sitk.Cast(imThresh*1024, sitk.sitkInt32),True)

    # Get the number of pixels that fall into each label map value
    cts = np.bincount(sitk.GetArrayFromImage(mask).flatten())

    # Keep only the largest 6 components for analysis
    maxVals = cts.argsort()[-6:][::-1]

    # Calculate metrics that describe segmentations
    PBR = np.zeros_like(maxVals, dtype=np.float32)
    NP = np.zeros_like(maxVals, dtype=np.float32)
    label_shape_analysis = sitk.LabelShapeStatisticsImageFilter()
    for i, val in enumerate(maxVals):
        binaryVol = sitk.Equal(mask, val.astype(np.float))
        label_shape_analysis.Execute(binaryVol)
        PBR[i] = label_shape_analysis.GetPerimeterOnBorderRatio(True)
        NP[i] = label_shape_analysis.GetNumberOfPixels(True)

    return NP, PBR, mask, maxVals


def AutoLungSegment(image, l = 0.05, u = 0.4, NPthresh=1e5):
    """
    Segments the lungs, generating a bounding box

    Args
        image (sitk.Image)  : the input image
        l (float)           : the lower (normalised) threshold
        u (float)           : the upper (normalised) threshold
        NPthresh (int)      : lower limit of voxel counts for a structure to be tested

    Returns
        maskBox (np.ndarray)    : bounding box of the automatically segmented lungs
        maskBinary (sitk.Image) : the segmented lungs (+/- airways)

    """

    # Normalise image intensity
    imNorm = sitk.Normalize(sitk.Threshold(image, -1000,500, outsideValue=-1000))

    # Calculate the label maps and metrics on non-connected regions
    NP, PBR, mask, labels = ThresholdAndMeasureLungVolume(imNorm,l,u)
    indices = np.array(np.where(np.logical_and(PBR<=5e-4, NP>NPthresh)))

    if indices.size==0:
        print("     Warning - non-zero perimeter/border ratio")
        indices = np.argmin(PBR)

    if indices.size==1:
        validLabels = labels[indices]
        maskBinary = sitk.Equal(mask, int(validLabels))

    else:
        validLabels = labels[indices[0]]
        maskBinary = sitk.Equal(mask, int(validLabels[0]))
        for i in range(len(validLabels)-1):
            maskBinary = sitk.Add(maskBinary, sitk.Equal(mask, int(validLabels[i+1])))
    maskBinary = maskBinary>0
    label_shape_analysis = sitk.LabelShapeStatisticsImageFilter()
    label_shape_analysis.Execute(maskBinary)
    maskBox = label_shape_analysis.GetBoundingBox(True)

    return maskBox, maskBinary

def CropImage(image, cropBox):
    """
    Crops an image using a bounding box

    Args
        image (sitk.Image)          : the input image
        cropBox (list, np.ndarray)  : the bounding box
                                      (sag0, cor0, ax0, sagD, corD, axD)

    Returns
        imCrop (sitk.Image)         : the cropped image

    """
    imCrop = sitk.RegionOfInterest(image, size=cropBox[3:], index=cropBox[:3])
    return imCrop

def vectorisedTransformIndexToPhysicalPoint(image, pointArr, correct=True):
    """
    Transforms a set of points from array indices to real-space
    """
    if correct:
        spacing = image.GetSpacing()[::-1]
        origin = image.GetOrigin()[::-1]
    else:
        spacing = image.GetSpacing()
        origin = image.GetOrigin()
    return pointArr*spacing + origin

def vectorisedTransformPhysicalPointToIndex(image, pointArr, correct=True):
    """
    Transforms a set of points from real-space to array indices
    """
    if correct:
        spacing = image.GetSpacing()[::-1]
        origin = image.GetOrigin()[::-1]
    else:
        spacing = image.GetSpacing()
        origin = image.GetOrigin()
    return (pointArr-origin)/spacing


def evaluateDistanceOnSurface(referenceVolume, testVolume, absDistance=True, referenceAsDistanceMap=False):
    """
    Evaluates a distance map on a surface
    Input: referenceVolume: binary volume SimpleITK image, or alternatively a distance map
           testVolume: binary volume SimpleITK image
    Output: theta, phi, values
    """
    if referenceAsDistanceMap:
        referenceDistanceMap = referenceVolume
    else:
        if absDistance:
            referenceDistanceMap = sitk.Abs(sitk.SignedMaurerDistanceMap(referenceVolume, squaredDistance=False, useImageSpacing=True))

        else:
            referenceDistanceMap = sitk.SignedMaurerDistanceMap(referenceVolume, squaredDistance=False, useImageSpacing=True)

    testSurface = sitk.LabelContour(testVolume)

    distanceImage = sitk.Multiply(referenceDistanceMap, sitk.Cast(testSurface, sitk.sitkFloat32))
    distanceArray = sitk.GetArrayFromImage(distanceImage)

    # Calculate centre of mass in real coordinates
    testSurfaceArray = sitk.GetArrayFromImage(testSurface)
    testSurfaceLocations = np.where(testSurfaceArray==1)
    testSurfaceLocationsArray = np.array(testSurfaceLocations)
    COMIndex = testSurfaceLocationsArray.mean(axis=1)
    COMReal = vectorisedTransformIndexToPhysicalPoint(testSurface, COMIndex)

    # Calculate each point on the surface in real coordinates
    pts = testSurfaceLocationsArray.T
    ptsReal = vectorisedTransformIndexToPhysicalPoint(testSurface, pts)
    ptsDiff = ptsReal - COMReal

    # Convert to spherical polar coordinates - base at north pole
    rho = np.sqrt((ptsDiff*ptsDiff).sum(axis=1))
    theta = np.pi/2.-np.arccos(ptsDiff.T[0]/rho)
    phi =  -1*np.arctan2(ptsDiff.T[2],-1.0*ptsDiff.T[1])

    # Extract values
    values = distanceArray[testSurfaceLocations]

    return theta, phi, values


def regridSphericalData(theta, phi, values, resolution):
    """
    Re-grids spherical data
    Input: theta, phi, values
    Options: plot a figure (plotFig), save a figure (saveFig), case identifier (figName)
    Output: pLat, pLong, gridValues (, fig)
    """
    # Re-grid:
    #  Set up grid
    Dradian = resolution*np.pi/180
    pLong, pLat = np.mgrid[-np.pi:np.pi:Dradian, -np.pi/2.:np.pi/2.0:Dradian]

    # First pass - linear interpolation, works well but not for edges
    gridValues = griddata(list(zip(theta, phi)), values, (pLat, pLong), method='linear', rescale=False)

    # Second pass - nearest neighbour interpolation
    gridValuesNN = griddata(list(zip(theta, phi)), values, (pLat, pLong), method='nearest', rescale=False)

    # Third pass - wherever the linear interpolation isn't defined use nearest neighbour interpolation
    gridValues[~np.isfinite(gridValues)] = gridValuesNN[~np.isfinite(gridValues)]

    return pLat, pLong, gridValues


def medianAbsoluteDeviation(data, axis=None):
    """ Median Absolute Deviation: a "Robust" version of standard deviation.
        Indices variabililty of the sample.
        https://en.wikipedia.org/wiki/Median_absolute_deviation
    """
    return np.median(np.abs(data - np.median(data, axis=axis)), axis=axis)

def norm(x, mean, sd):
    norm = []
    for i in range(x.size):
        norm += [1.0/(sd*np.sqrt(2*np.pi))*np.exp(-(x[i] - mean)**2/(2*sd**2))]
    return np.array(norm)

def res(p, y, x):
    m, dm, sd1, sd2 = p
    m1 = m
    m2 = m1 + dm
    y_fit = norm(x, m1, sd1) + norm(x, m2, sd2)
    err = y - y_fit
    return err

def gaussianCurve(x, a, m, s):
    return a*scipy_norm.pdf(x, loc=m, scale=s)

def IAR(atlasSet, structureName, smoothMaps=False, smoothSigma=1, zScore='MAD', outlierMethod='IQR', minBestAtlases=10, N_factor=1.5, logFile='IAR_{0}.log'.format(datetime.datetime.now()), debug=False, iteration=0, singleStep=False):

    if iteration == 0:
        # Run some checks in the data?

        # Begin the process
        print('Iterative atlas removal: ')
        print('  Beginning process')
        logFile = open(logFile, 'w')
        logFile.write('Iteration,Atlases,Qvalue,Threshold\n')

    # Get remaining case identifiers to loop through
    remainingIdList = list(atlasSet.keys())

    #Modify resolution for better statistics
    if len(remainingIdList)<12:
        print('  Less than 12 atlases, resolution set: 3x3 sqr deg')
        resolution = 3
    elif len(remainingIdList)<7:
        print('  Less than 7 atlases, resolution set: 6x6 sqr deg')
        resolution = 6
    else:
        resolution = 1

    # Generate the surface projections
    #   1. Set the consensus surface using the reference volume
    probabilityLabel = combineLabels(atlasSet, structureName)[structureName]
    referenceVolume = processProbabilityImage(probabilityLabel, threshold=1)
    referenceDistanceMap = sitk.Abs(sitk.SignedMaurerDistanceMap(referenceVolume, squaredDistance=False, useImageSpacing=True))

    gValList = []
    print('  Calculating surface distance maps: ')
    #print('    ', end=' ')
    for testId in remainingIdList:
        print('    {0}'.format(testId), end=" ")
        sys.stdout.flush()
        #   2. Calculate the distance from the surface to the consensus surface

        testVolume = atlasSet[testId]['DIR'][structureName]

        # This next step ensures non-binary labels are treated properly
        # We use 0.1 to capture the outer edge of the test delineation, if it is probabilistic
        testVolume = processProbabilityImage(testVolume, 0.1)

        # Now compute the distance across the surface
        theta, phi, values = evaluateDistanceOnSurface(referenceDistanceMap, testVolume, referenceAsDistanceMap=True)
        pLat, pLong, gVals = regridSphericalData(theta, phi, values, resolution=resolution)

        gValList.append(gVals)
    print()
    QResults = {}

    for i, (testId, gVals) in enumerate(zip(remainingIdList, gValList)):

        gValListTest = gValList[:]
        gValListTest.pop(i)

        if smoothMaps:
            gVals = filters.gaussian_filter(gVals, sigma=smoothSigma, mode='wrap')

        #       b) i] Compute the Z-scores over the projected surface
        if zScore.lower()=='std':
            gValMean = np.mean(gValListTest, axis=0)
            gValStd = np.std(gValListTest, axis=0)

            if np.any(gValStd==0):
                print('    Std Dev zero count: {0}'.format(np.sum(gValStd==0)))
                gValStd[gValStd==0] = gValStd.mean()

            zScoreValsArr =  ( gVals - gValMean ) / gValStd

        elif zScore.lower()=='mad':
            gValMedian = np.median(gValListTest, axis=0)
            gValMAD    = 1.4826 * medianAbsoluteDeviation(gValListTest, axis=0)

            if np.any(~np.isfinite(gValMAD)):
                print('Error in MAD')
                print(gValMAD)

            if np.any(gValMAD==0):
                print('    MAD zero count: {0}'.format(np.sum(gValMAD==0)))
                gValMAD[gValMAD==0] = np.median(gValMAD)

            zScoreValsArr =  ( gVals - gValMedian ) / gValMAD

        else:
            print(' Error!')
            print(' zScore must be one of: MAD, STD')
            sys.exit()

        zScoreVals = np.ravel( zScoreValsArr )

        if debug:
            print('      [{0}] Statistics of mZ-scores'.format(testId))
            print('        Min(Z)    = {0:.2f}'.format(zScoreVals.min()))
            print('        Q1(Z)     = {0:.2f}'.format(np.percentile(zScoreVals, 25)))
            print('        Mean(Z)   = {0:.2f}'.format(zScoreVals.mean()))
            print('        Median(Z) = {0:.2f}'.format(np.percentile(zScoreVals, 50)))
            print('        Q3(Z)     = {0:.2f}'.format(np.percentile(zScoreVals, 75)))
            print('        Max(Z)    = {0:.2f}\n'.format(zScoreVals.max()))

        # Calculate excess area from Gaussian: the Q-metric
        bins = np.linspace(-15,15,501)
        zDensity, bin_edges = np.histogram(zScoreVals, bins=bins, density=True)
        bin_centers = (bin_edges[1:]+bin_edges[:-1])/2.0

        popt, pcov = curve_fit(f=gaussianCurve, xdata=bin_centers, ydata=zDensity)
        zIdeal = gaussianCurve(bin_centers, *popt)
        zDiff = np.abs(zDensity - zIdeal)

        # Integrate to get the Q_value
        Q_value = np.trapz(zDiff*np.abs(bin_centers)**2, bin_centers)
        QResults[testId] = np.float64(Q_value)

    # Exclude (at most) the worst 3 atlases for outlier detection
    # With a minimum number, this helps provide more robust estimates at low numbers
    RL = list(QResults.values())
    bestResults = np.sort(RL)[:max([minBestAtlases, len(RL)-3])]

    if outlierMethod.lower()=='iqr':
        outlierLimit = np.percentile(bestResults, 75, axis=0) + N_factor*np.subtract(*np.percentile(bestResults, [75, 25], axis=0))
    elif outlierMethod.lower()=='std':
        outlierLimit = np.mean(bestResults, axis=0) + N_factor*np.std(bestResults, axis=0)
    else:
        print(' Error!')
        print(' outlierMethod must be one of: IQR, STD')
        sys.exit()

    print('  Analysing results')
    print('   Outlier limit: {0:06.3f}'.format(outlierLimit))
    keepIdList = []

    logFile.write('{0},{1},{2},{3:.4g}\n'.format(iteration,
                                                 ' '.join(remainingIdList),
                                                 ' '.join(['{0:.4g}'.format(i) for i in list(QResults.values())]),
                                                 outlierLimit))
    logFile.flush()

    for ii, result in QResults.items():

        accept = (result <= outlierLimit)

        print('      {0}: Q = {1:06.3f} [{2}]'.format(ii, result, {True:'KEEP',False:'REMOVE'}[accept]))

        if accept:
            keepIdList.append(ii)

    if len(keepIdList)<len(remainingIdList):
        print('\n  Step {0} Complete'.format(iteration))
        print('   Num. Removed = {0} --\n'.format(len(remainingIdList)-len(keepIdList)))

        iteration += 1
        atlasSetNew = {i:atlasSet[i] for i in keepIdList}

        if singleStep:
            return atlasSetNew
        else:
            return IAR(atlasSet=atlasSetNew, structureName=structureName, smoothMaps=smoothMaps, smoothSigma=smoothSigma, zScore=zScore, outlierMethod=outlierMethod, minBestAtlases=minBestAtlases, N_factor=N_factor, logFile=logFile, debug=debug, iteration=iteration)

    else:
        print('  End point reached. Keeping:\n   {0}'.format(keepIdList))
        logFile.close()

        return atlasSet

def processProbabilityImage(probabilityImage, threshold=0.5):

    # Check type
    if type(probabilityImage)!=sitk.Image:
        probabilityImage = sitk.GetImageFromArray(probabilityImage)

    # Normalise probability map
    probabilityImage = (probabilityImage / sitk.GetArrayFromImage(probabilityImage).max())

    # Get the starting binary image
    binaryImage = sitk.BinaryThreshold(probabilityImage, lowerThreshold=threshold)

    # Fill holes
    binaryImage = sitk.BinaryFillhole(binaryImage)

    # Apply the connected component filter
    labelledImage = sitk.ConnectedComponent(binaryImage)

    # Measure the size of each connected component
    labelShapeFilter = sitk.LabelShapeStatisticsImageFilter()
    labelShapeFilter.Execute(labelledImage)
    labelIndices = labelShapeFilter.GetLabels()
    voxelCounts  = [labelShapeFilter.GetNumberOfPixels(i) for i in labelIndices]
    if voxelCounts==[]:
        return binaryImage

    # Select the largest region
    largestComponentLabel = labelIndices[np.argmax(voxelCounts)]
    largestComponentImage = (labelledImage==largestComponentLabel)

    return sitk.Cast(largestComponentImage, sitk.sitkUInt8)


def COMFromImageList(sitkImageList, conditionType="count", conditionValue=0, scanDirection = 'z'):
    """
    Input: list of SimpleITK images
           minimum total slice area required for the tube to be inserted at that slice
           scan direction: x = sagittal, y=coronal, z=axial
    Output: mean centre of mass positions, with shape (NumSlices, 2)
    Note: positions are converted into image space by default
    """
    if scanDirection.lower()=='x':
        print("Scanning in sagittal direction")
        COMZ = []
        COMY = []
        W    = []
        C    = []

        referenceImage = sitkImageList[0]
        referenceArray = sitk.GetArrayFromImage(referenceImage)
        z,y = np.mgrid[0:referenceArray.shape[0]:1, 0:referenceArray.shape[1]:1]

        with np.errstate(divide='ignore', invalid='ignore'):
            for sitkImage in sitkImageList:
                volumeArray = sitk.GetArrayFromImage(sitkImage)
                comZ = 1.0*(z[:,:,np.newaxis]*volumeArray).sum(axis=(1,0))
                comY = 1.0*(y[:,:,np.newaxis]*volumeArray).sum(axis=(1,0))
                weights = np.sum(volumeArray, axis=(1,0))
                W.append(weights)
                C.append(np.any(volumeArray, axis=(1,0)))
                comZ/=(1.0*weights)
                comY/=(1.0*weights)
                COMZ.append(comZ)
                COMY.append(comY)

        with warnings.catch_warnings():
            """
            It's fairly likely some slices have just np.NaN values - it raises a warning but we can suppress it here
            """
            warnings.simplefilter("ignore", category=RuntimeWarning)
            meanCOMZ = np.nanmean(COMZ, axis=0)
            meanCOMY = np.nanmean(COMY, axis=0)
            if conditionType.lower()=="area":
                meanCOM = np.dstack((meanCOMZ, meanCOMY))[0]*np.array((np.sum(W, axis=0)>(conditionValue),)*2).T
            elif conditionType.lower()=="count":
                meanCOM = np.dstack((meanCOMZ, meanCOMY))[0]*np.array((np.sum(C, axis=0)>(conditionValue),)*2).T
            else:
                print("Invalid condition type, please select from 'area' or 'count'.")
                sys.exit()

        pointArray = []
        for index, COM in enumerate(meanCOM):
            if np.all(np.isfinite(COM)):
                if np.all(COM>0):
                    pointArray.append(referenceImage.TransformIndexToPhysicalPoint(( index, int(COM[1]), int(COM[0]))))

        return pointArray

    elif scanDirection.lower()=='z':
        print("Scanning in axial direction")
        COMX = []
        COMY = []
        W    = []
        C    = []

        referenceImage = sitkImageList[0]
        referenceArray = sitk.GetArrayFromImage(referenceImage)
        x,y = np.mgrid[0:referenceArray.shape[1]:1, 0:referenceArray.shape[2]:1]

        with np.errstate(divide='ignore', invalid='ignore'):
            for sitkImage in sitkImageList:
                volumeArray = sitk.GetArrayFromImage(sitkImage)
                comX = 1.0*(x*volumeArray).sum(axis=(1,2))
                comY = 1.0*(y*volumeArray).sum(axis=(1,2))
                weights = np.sum(volumeArray, axis=(1,2))
                W.append(weights)
                C.append(np.any(volumeArray, axis=(1,2)))
                comX/=(1.0*weights)
                comY/=(1.0*weights)
                COMX.append(comX)
                COMY.append(comY)

        with warnings.catch_warnings():
            """
            It's fairly likely some slices have just np.NaN values - it raises a warning but we can suppress it here
            """
            warnings.simplefilter("ignore", category=RuntimeWarning)
            meanCOMX = np.nanmean(COMX, axis=0)
            meanCOMY = np.nanmean(COMY, axis=0)
            if conditionType.lower()=="area":
                meanCOM = np.dstack((meanCOMX, meanCOMY))[0]*np.array((np.sum(W, axis=0)>(conditionValue),)*2).T
            elif conditionType.lower()=="count":
                meanCOM = np.dstack((meanCOMX, meanCOMY))[0]*np.array((np.sum(C, axis=0)>(conditionValue),)*2).T
            else:
                print("Invalid condition type, please select from 'area' or 'count'.")
                quit()
        pointArray = []
        for index, COM in enumerate(meanCOM):
            if np.all(np.isfinite(COM)):
                if np.all(COM>0):
                    pointArray.append(referenceImage.TransformIndexToPhysicalPoint((int(COM[1]), int(COM[0]), index)))

        return pointArray

def tubeFromCOMList(COMList, radius):
    """
    Input: image-space positions along the tube centreline.
    Output: VTK tube
    Note: positions do not have to be continuous - the tube is interpolated in real space
    """
    points = vtk.vtkPoints()
    for i,pt in enumerate(COMList):
        points.InsertPoint(i, pt[0], pt[1], pt[2])

    # Fit a spline to the points
    print("Fitting spline")
    spline = vtk.vtkParametricSpline()
    spline.SetPoints(points)

    functionSource = vtk.vtkParametricFunctionSource()
    functionSource.SetParametricFunction(spline)
    functionSource.SetUResolution(10 * points.GetNumberOfPoints())
    functionSource.Update()

    # Generate the radius scalars
    tubeRadius = vtk.vtkDoubleArray()
    n = functionSource.GetOutput().GetNumberOfPoints()
    tubeRadius.SetNumberOfTuples(n)
    tubeRadius.SetName("TubeRadius")
    for i in range(n):
        # We can set the radius based on the given propagated segmentations in that slice?
        # Typically segmentations are elliptical, this could be an issue so for now a constant radius is used
        tubeRadius.SetTuple1(i, radius)

    # Add the scalars to the polydata
    tubePolyData = vtk.vtkPolyData()
    tubePolyData = functionSource.GetOutput()
    tubePolyData.GetPointData().AddArray(tubeRadius)
    tubePolyData.GetPointData().SetActiveScalars("TubeRadius")

    # Create the tubes
    tuber = vtk.vtkTubeFilter()
    tuber.SetInputData(tubePolyData)
    tuber.SetNumberOfSides(50)
    tuber.SetVaryRadiusToVaryRadiusByAbsoluteScalar()
    tuber.Update()

    return tuber

def writeVTKTubeToFile(tube, filename):
    """
    Input: VTK tube
    Output: exit success
    Note: format is XML VTP
    """
    print("Writing tube to polydata file (VTP)")
    polyDataWriter = vtk.vtkXMLPolyDataWriter()
    polyDataWriter.SetInputData(tube.GetOutput())

    polyDataWriter.SetFileName(filename)
    polyDataWriter.SetCompressorTypeToNone()
    polyDataWriter.SetDataModeToAscii()
    s = polyDataWriter.Write()

    return s

def SimpleITKImageFromVTKTube(tube, SITKReferenceImage, verbose = False):
    """
    Input: VTK tube, referenceImage (used for spacing, etc.)
    Output: SimpleITK image
    Note: Uses binary output (background 0, foreground 1)
    """
    size     = list(SITKReferenceImage.GetSize())
    origin   = list(SITKReferenceImage.GetOrigin())
    spacing  = list(SITKReferenceImage.GetSpacing())
    ncomp    = SITKReferenceImage.GetNumberOfComponentsPerPixel()

    # convert the SimpleITK image to a numpy array
    arr = sitk.GetArrayFromImage(SITKReferenceImage).transpose(2,1,0).flatten()

    # send the numpy array to VTK with a vtkImageImport object
    dataImporter = vtk.vtkImageImport()

    dataImporter.CopyImportVoidPointer( arr, len(arr) )
    dataImporter.SetDataScalarTypeToUnsignedChar()
    dataImporter.SetNumberOfScalarComponents(ncomp)

    # Set the new VTK image's parameters
    dataImporter.SetDataExtent (0, size[0]-1, 0, size[1]-1, 0, size[2]-1)
    dataImporter.SetWholeExtent(0, size[0]-1, 0, size[1]-1, 0, size[2]-1)
    dataImporter.SetDataOrigin(origin)
    dataImporter.SetDataSpacing(spacing)

    dataImporter.Update()

    VTKReferenceImage = dataImporter.GetOutput()

    # fill the image with foreground voxels:
    inval = 1
    outval = 0
    count = VTKReferenceImage.GetNumberOfPoints()
    VTKReferenceImage.GetPointData().GetScalars().Fill(inval)

    if verbose:
        print("Generating volume using extrusion.")
    extruder = vtk.vtkLinearExtrusionFilter()
    extruder.SetInputData(tube.GetOutput())

    extruder.SetScaleFactor(1.)
    extruder.SetExtrusionTypeToNormalExtrusion()
    extruder.SetVector(0, 0, 1)
    extruder.Update()

    if verbose:
        print("Using polydaya to generate stencil.")
    pol2stenc = vtk.vtkPolyDataToImageStencil()
    pol2stenc.SetTolerance(0) # important if extruder.SetVector(0, 0, 1) !!!
    pol2stenc.SetInputConnection(tube.GetOutputPort())
    pol2stenc.SetOutputOrigin(VTKReferenceImage.GetOrigin())
    pol2stenc.SetOutputSpacing(VTKReferenceImage.GetSpacing())
    pol2stenc.SetOutputWholeExtent(VTKReferenceImage.GetExtent())
    pol2stenc.Update()

    if verbose:
        print("using stencil to generate image.")
    imgstenc = vtk.vtkImageStencil()
    imgstenc.SetInputData(VTKReferenceImage)
    imgstenc.SetStencilConnection(pol2stenc.GetOutputPort())
    imgstenc.ReverseStencilOff()
    imgstenc.SetBackgroundValue(outval)
    imgstenc.Update()

    if verbose:
        print("Generating SimpleITK image.")
    finalImage = imgstenc.GetOutput()
    finalArray = finalImage.GetPointData().GetScalars()
    finalArray = vtk_to_numpy(finalArray).reshape(SITKReferenceImage.GetSize()[::-1])
    finalImageSITK = sitk.GetImageFromArray(finalArray)
    finalImageSITK.CopyInformation(SITKReferenceImage)

    return finalImageSITK

def ConvertSimpleITKtoVTK(img):
    """

    """
    size     = list(img.GetSize())
    origin   = list(img.GetOrigin())
    spacing  = list(img.GetSpacing())
    ncomp    = img.GetNumberOfComponentsPerPixel()

    # convert the SimpleITK image to a numpy array
    arr = sitk.GetArrayFromImage(img).transpose(2,1,0).flatten()
    arr_string = arr.tostring()

    # send the numpy array to VTK with a vtkImageImport object
    dataImporter = vtk.vtkImageImport()

    dataImporter.CopyImportVoidPointer( arr_string, len(arr_string) )
    dataImporter.SetDataScalarTypeToUnsignedChar()
    dataImporter.SetNumberOfScalarComponents(ncomp)

    # Set the new VTK image's parameters
    dataImporter.SetDataExtent (0, size[0]-1, 0, size[1]-1, 0, size[2]-1)
    dataImporter.SetWholeExtent(0, size[0]-1, 0, size[1]-1, 0, size[2]-1)
    dataImporter.SetDataOrigin(origin)
    dataImporter.SetDataSpacing(spacing)

    dataImporter.Update()

    vtk_image = dataImporter.GetOutput()
    return vtk_image

def vesselSplineGeneration(atlasSet, vesselNameList, vesselRadiusDict, stopConditionTypeDict, stopConditionValueDict, scanDirectionDict):
    """

    """
    splinedVessels = {}
    for vesselName in vesselNameList:

        imageList    = [atlasSet[i]['DIR'][vesselName] for i in atlasSet.keys()]

        vesselRadius        = vesselRadiusDict[vesselName]
        stopConditionType   = stopConditionTypeDict[vesselName]
        stopConditionValue  = stopConditionValueDict[vesselName]
        scanDirection       = scanDirectionDict[vesselName]

        pointArray = COMFromImageList(imageList, conditionType=stopConditionType, conditionValue=stopConditionValue, scanDirection=scanDirection)
        tube       = tubeFromCOMList(pointArray, radius=vesselRadius)

        SITKReferenceImage  = imageList[0]

        splinedVessels[vesselName] = SimpleITKImageFromVTKTube(tube, SITKReferenceImage, verbose = False)
    return splinedVessels