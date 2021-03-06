from calc.fitting import fitEllipsoid
from calc.stat import communityDistanceStats, communityOrientationStats
from data.util import NoBacteria
from render.ibc import IBCRenderer
from render.bacteria import BacteriaLayer
from render.basic import boolInt
from store import DataStore
from vector import Vec3f
from wxVTK.wxVTKRenderWindowInteractor import wxVTKRenderWindowInteractor

import math
import vtk
import wx

class IBCRenderPanel(wx.Panel):
    """
    The panel class used for displaying and interacting with 3D microscope data.
    
    :@type imode_callback: func
    :@param imode_callback: Sets the interaction mode status.    
    :@type rmode_callback: func
    :@param rmode_callback: Sets the recording/exploring mode status.
    :@type ppos_callback: func
    :@param ppos_callback: Sets the status of the 3D location of the mouse.
    :@type ao: func
    :@param ao: Updates the output text area on the main frame.
    """
    def __init__( self, parent, imode_callback, rmode_callback, ppos_callback, ao, **kwargs ):
        # initialize Panel
        if 'id' not in kwargs:
            kwargs['id'] = wx.ID_ANY
        wx.Panel.__init__( self, parent, **kwargs )

        self.setInteractionMode = imode_callback
        self.setInteractionMode(True)
        
        self.recordingMode = False
        self.setRecordingMode = rmode_callback
        self.setRecordingMode(False)
        
        self.setPickerPos = ppos_callback
        
        self.ao = ao
        self.aa = False
        self.firstRender = True

        self.vtkWidget = wxVTKRenderWindowInteractor(self, wx.ID_ANY)
        self.iren = self.vtkWidget._Iren
        self.iren.SetInteractorStyle(vtk.vtkInteractorStyleTrackballCamera())
        
        self.renderer = vtk.vtkRenderer()
        self.renderer.SetBackground(0,0,0)
        self.imageLayer = {}
        self.CISID = -1  # Current Image Set ID
        self.imageLayer[self.CISID] = IBCRenderer(self.renderer, self.iren.Render)
        self.bacteriaLayer = BacteriaLayer(self.renderer, self.iren.Render)
        
        self.viewCamActive = True
        
        # for interactive clipping
        self.planes = vtk.vtkPlanes()
        
        self.ellipsoid = None
        self.ellipsoidTextActor = None
        
        # The SetInteractor method is how 3D widgets are associated with the
        # render window interactor. Internally, SetInteractor sets up a bunch
        # of callbacks using the Command/Observer mechanism (AddObserver()).
        self.boxWidget = vtk.vtkBoxWidget()
        self.boxWidget.SetInteractor(self.iren)
        self.boxWidget.SetPlaceFactor(1.0)

        # init vtk window
        self.vtkWidget.Enable(1)
        self.vtkWidget.AddObserver("ExitEvent", lambda o,e,f=parent: f.Close())
        self.vtkWidget.GetRenderWindow().AddRenderer(self.renderer)
        
        # Bind VTK events
        self.iren.AddObserver("KeyPressEvent", self.OnKeyDown)
        
        self.Sizer = wx.BoxSizer()
        self.Sizer.Add(self.vtkWidget, 1, wx.EXPAND)
        

    def initPicker(self):
        coneSource = vtk.vtkConeSource()
        coneSource.CappingOn()
        coneSource.SetHeight(2)
        coneSource.SetRadius(1)
        coneSource.SetResolution(10)
        coneSource.SetCenter(1,0,0)
        coneSource.SetDirection(-1,0,0)
        
        coneMapper = vtk.vtkDataSetMapper()
        coneMapper.SetInputConnection(coneSource.GetOutputPort())
        
        self.redCone = vtk.vtkActor()
        self.redCone.PickableOff()
        self.redCone.SetMapper(coneMapper)
        self.redCone.GetProperty().SetColor(1,0,0)
        
        self.greenCone = vtk.vtkActor()
        self.greenCone.PickableOff()
        self.greenCone.SetMapper(coneMapper)
        self.greenCone.GetProperty().SetColor(0,1,0)
        
        # Add the two cones (or just one, if you want)
        self.renderer.AddViewProp(self.redCone)
        self.renderer.AddViewProp(self.greenCone)
        
        self.picker = vtk.vtkVolumePicker()
        self.picker.SetTolerance(1e-6)
        self.picker.SetVolumeOpacityIsovalue(0.1)
        
    def _pickerVisibility(self, visible):
        self.redCone.SetVisibility(boolInt(visible))
        self.greenCone.SetVisibility(boolInt(visible))
        
    def initBoxWidgetInteraction(self, imageOutput):
        # set up interaction handling
        self.boxWidget.SetInput(imageOutput)
        self.boxWidget.PlaceWidget()
        self.boxWidget.InsideOutOn()
        self.boxWidget.AddObserver("StartInteractionEvent", self.StartInteraction)
        self.boxWidget.AddObserver("InteractionEvent", self.ClipVolumeRender)
        self.boxWidget.AddObserver("EndInteractionEvent", self.EndInteraction)
        
        # set up box widget representation
        outlineProperty = self.boxWidget.GetOutlineProperty()
        outlineProperty.SetRepresentationToWireframe()
        outlineProperty.SetAmbient(1.0)
        outlineProperty.SetAmbientColor(1, 1, 1)
        outlineProperty.SetLineWidth(3)
        selectedOutlineProperty = self.boxWidget.GetSelectedOutlineProperty()
        selectedOutlineProperty.SetRepresentationToWireframe()
        selectedOutlineProperty.SetAmbient(1.0)
        selectedOutlineProperty.SetAmbientColor(1, 0, 0)
        selectedOutlineProperty.SetLineWidth(3)
    

    def RenderImageData(self, ID, imgReader):
        # check if this is the first loaded image set
        if self.CISID == -1:
            self.imageLayer = {}
        
        self.CISID = ID
        self.imageLayer[self.CISID] = IBCRenderer(self.renderer, self.iren.Render)
        self.imageLayer[self.CISID].SetImageSet(ID)
        locator = self.imageLayer[self.CISID].Render(imgReader)
        self.initPicker()
        self.picker.AddLocator(locator)
        self.initBoxWidgetInteraction(imgReader.VolumeReader.GetOutput())
        
        if self.firstRender:
            self.iren.AddObserver("MouseMoveEvent", self.MoveCursor)
            self.iren.AddObserver("LeftButtonPressEvent", self.LeftClick)
            self.iren.AddObserver("RightButtonPressEvent", self.RightClick)
            
            # It is convenient to create an initial view of the data. The FocalPoint
            # and Position form a vector direction. Later on (ResetCamera() method)
            # this vector is used to position the camera to look at the data in
            # this direction.
            self.viewCam = vtk.vtkCamera()
            self.viewCam.SetViewUp(0, 0, -1)
            self.viewCam.SetPosition(0, 1.1, 2)
            self.viewCam.SetFocalPoint(0, -0.25, 0)
            self.viewCam.ComputeViewPlaneNormal()
            
            # This camera should generally stay stationary, 
            # and only be used for taking screenshots
            self.picCam = vtk.vtkCamera()
            self.picCam.SetViewUp(0, 0, -1)
            self.picCam.SetPosition(0, 1.1, 2)
            self.picCam.SetFocalPoint(0, -0.25, 0)
            self.picCam.ComputeViewPlaneNormal()
            
            # Actors are added to the renderer. An initial camera view is created.
            # The Dolly() method moves the camera towards the FocalPoint,
            # thereby enlarging the image.
            self.renderer.SetActiveCamera(self.viewCam)
            self.renderer.ResetCamera() 
            self.viewCam.Dolly(1.0)
            self.renderer.ResetCameraClippingRange()
            self.iren.Render()
            
            self.firstRender = False
        
    
    def CaptureCamera(self):
        cam = self.renderer.GetActiveCamera()
        camDict = {"Position": cam.GetPosition()}
        camDict["FocalPoint"]= cam.GetFocalPoint()
        camDict["ViewAngle"] = cam.GetViewAngle()
        camDict["ViewUp"] = cam.GetViewUp()
        camDict["ClippingRange"] = cam.GetClippingRange()
        camDict["ParallelScale"] = cam.GetParallelScale()
        
        return camDict
    
    def RestoreCamera(self, camDict):
        cam = self.renderer.GetActiveCamera()
        cam.SetPosition(camDict["Position"])
        cam.SetFocalPoint(camDict["FocalPoint"])
        cam.SetViewAngle(camDict["ViewAngle"])
        cam.SetViewUp(camDict["ViewUp"])
        cam.SetClippingRange(camDict["ClippingRange"])
        cam.SetParallelScale(camDict["ParallelScale"])

    def RecordBacterium(self):
        """
        Creates an internal representation of a bacterium as well as 
        creating a vtkActor representation and stores both in the DataStore, 
        then refreshes the render window. 
        """
        self.bacteriaLayer.AddBacterium()
        self.iren.Render()
        
    def RenderStoredBacteria(self):
        self.bacteriaLayer.AddStoredBacteria()
        self.renderer.Render()
        
    def RenderStoredMarkers(self):
        markers = list(DataStore.Markers())
        DataStore.ClearMarkers()
        
        for marker in markers:
            actor = self.bacteriaLayer.CreateMarker(marker)
            DataStore.AddMarker(actor)
            self.renderer.AddActor(actor)
        self.renderer.Render()
        
    def DeleteBacterium(self, idx=None):
        """
        Delete the recorded bacterium (internal and actor) 
        at the specified index.
        
        Note: if no index is passed, the most recently added 
              bacterium is deleted.
        
        :@type idx: int
        :@param idx: The index of the bacterium to delete 
        """
        if (not len(DataStore.Bacteria())):
            return
        
        if idx is None:
            idx = -1
        
        # remove actor from renderer
        self.renderer.RemoveActor(DataStore.BacteriaActors()[idx])
        
        del DataStore.Bacteria()[idx]
        del DataStore.BacteriaActors()[idx]
        
        self.iren.Render()

    def RenderFittedEllipsoid(self, mve):
        # if fit already exists, clear b/f fitting again
        if self.ellipsoid:
            self.renderer.RemoveActor(self.ellipsoid)
            
        try:
            ds = Vec3f(self.imageLayer[self.CISID].dataSpacing)
            ar = self.bacteriaLayer.actor_radius
            self.ellipsoid, out = fitEllipsoid(ds, ar, mve)
            self.ao(out)
            self.renderer.AddActor(self.ellipsoid)
            self.iren.Render()
        except RuntimeError, re:
            wx.MessageBox(str(re), "Fitting Error", wx.ICON_ERROR | wx.OK)
    
    def ToggleEllipsoidVisibility(self):
        vstate = [1,0]
        if self.ellipsoid:
            self.ellipsoid.SetVisibility(vstate[self.ellipsoid.GetVisibility()])
            
    def CalculateOrientations(self):
        if NoBacteria(): return
        
        ds = communityOrientationStats()
        
        for s in ds:
            self.ao(str(s))
            
    def ColorByOrientation(self, colorScheme=None):
        if NoBacteria(): return
        
        bacilli, filaments, bdots, fdots, sRes = communityOrientationStats()
        bdots = [map(abs, bdots[i]) for i in range(3)]
        fdots = [map(abs, fdots[i]) for i in range(3)]
        
        if colorScheme is None:
            colorScheme = Vec3f(2,1,0)
        
        for i, a in enumerate(bacilli):
            aColl = vtk.vtkPropCollection()
            a.GetActors(aColl)
            aColl.InitTraversal()
            actors = [aColl.GetNextProp() for _ in range(aColl.GetNumberOfItems())]
            
            for actor in actors:
                actor.GetProperty().SetDiffuseColor(bdots[colorScheme.x][i], 
                                                    bdots[colorScheme.y][i], 
                                                    bdots[colorScheme.z][i])
        for j, fID in enumerate(filaments):
            fColl = vtk.vtkPropCollection()
            DataStore.BacteriaActors()[fID].GetActors(fColl)
            fColl.InitTraversal()
            factors = [fColl.GetNextProp() for _ in range(fColl.GetNumberOfItems())]
            
            # Set color LUT for filament spline based on marker positions
            colorTransferFunction = vtk.vtkColorTransferFunction()
            
            fdotIdx = sum(sRes[:j])
            for k in range(0, sRes[j]):
                colorTransferFunction.AddRGBPoint(k,fdots[colorScheme.x][fdotIdx+k],
                                                    fdots[colorScheme.y][fdotIdx+k],
                                                    fdots[colorScheme.z][fdotIdx+k])
#                print 'RGB:',fdots[colorScheme.x][fdotIdx+k],fdots[colorScheme.y][fdotIdx+k],fdots[colorScheme.z][fdotIdx+k]
            # filament spline
            factors[1].GetMapper().SetLookupTable(colorTransferFunction)
            factors[1].GetMapper().ScalarVisibilityOn()
            factors[1].GetMapper().SetColorModeToMapScalars()
            factors[1].GetMapper().InterpolateScalarsBeforeMappingOff()
            
            # sphere caps
            self.setColor(factors[0], fdotIdx, fdots, colorScheme)
            self.setColor(factors[-1], fdotIdx+k, fdots, colorScheme)
            

        self.iren.Render()
        
    def setColor(self, actor, idx, dots, cs):
        actor.GetProperty().SetDiffuseColor(dots[cs.x][idx], 
                                            dots[cs.y][idx], 
                                            dots[cs.z][idx])
    

    def CalculateCommunityDensity(self):
        if NoBacteria(): return
        ds = communityDistanceStats()
        self.ao(str(ds))
    
    # ACCESSORS/MODIFIERS
    def GetImageLayerByID(self, ID):
        if ID in self.imageLayer:
            return self.imageLayer[ID]
        return None
    
    def SetCurrentImageLayer(self, ID):
        if ID in self.imageLayer:
            self.CISID = ID
    
    @property
    def ImageLayer(self):
        """
        Returns a reference to the current IBCRenderer class that 
        tesselates the data into a vtkActor.
        
        :@rtype: render.ibc.IBCRenderer
        """
        return self.imageLayer[self.CISID]
    
    @property
    def BacteriaLayer(self):
        """
        Returns a reference to the BacteriaLayer class that 
        handles storing and representing user-placed markers 
        and bacteria.
        """
        return self.bacteriaLayer
    
    @property
    def BacteriaRenderer(self):
        """
        Returns a reference to the vtkRenderer layer that 
        recorded bacteria are drawn into.
        
        :@rtype: vtk.vtkRenderer
        """
        return self.renderer
#        return self.bactRenderer


    # MOUSE HANDLING
    def LeftClick(self, iren, event):
        if self.recordingMode:
            pos = Vec3f(self.picker.GetPickPosition())
            actor = self.bacteriaLayer.CreateMarker(pos)
            self.renderer.AddActor(actor)
            DataStore.AddMarker(actor)
            self.iren.Render()
    
    def RightClick(self, iren, event):
        pos = Vec3f(self.picker.GetPickPosition())
        minDist = -1
        minMarker = None
        mid = None
        
        if not len(DataStore.Markers()):
            return
        
        # find the closest marker to the click position
        for i, marker in enumerate(DataStore.Markers()):
            mpos = Vec3f(marker.GetCenter())
            dlen = (pos - mpos).length()
            if minDist < 0 or dlen < minDist:
                minDist = dlen
                minMarker = marker
                mid = i
        
        # make sure the user clicked somewhere near a marker before removing
        if minDist <= self.bacteriaLayer.actor_radius * 2:
            self.renderer.RemoveActor(minMarker)
            del DataStore.Markers()[mid]
            
            self.iren.Render()

    
    # KEY PRESS EVENT HANDLING
    def OnKeyDown(self, iren, event):
        key = iren.GetKeyCode().upper()
        if key == 'T':
            self.setInteractionMode()
        elif key == 'J':
            self.setInteractionMode(False)
        elif key == 'X':
            if self.recordingMode:
                self.recordingMode = False
                self.setRecordingMode(self.recordingMode)
            else:
                self.recordingMode = True
                self.setRecordingMode()
        elif key == 'D':
            self.OnDeleteRequest()
        elif key == 'C':
            self.switchCameras()
        elif key == 'A':
            if self.aa:
                self.aa = False
                self.iren.GetRenderWindow().SetAAFrames(0)
            else:
                self.aa = True
                self.iren.GetRenderWindow().SetAAFrames(16)
            
    
    def switchCameras(self):
        if self.viewCamActive:
            self.viewCamActive = False
            self.renderer.SetActiveCamera(self.picCam)
        else:
            self.viewCamActive = True
            self.renderer.SetActiveCamera(self.viewCam)
    
    def OnDeleteRequest(self):
        minDist = -1
        bactID = None
        
        if not len(DataStore.Bacteria()):
            return
        
        pos = Vec3f(self.picker.GetPickPosition())
        # find the closest bacterium to the click position
        for i, bact in enumerate(DataStore.Bacteria()):
            for _, marker in enumerate(bact.Markers):
                diff = pos - marker
                dlen = diff.length()
                if minDist < 0 or dlen < minDist:
                    minDist = diff.length()
                    bactID = i
        
        # make sure the user clicked somewhere near a marker before removing
        if minDist <= self.bacteriaLayer.actor_radius * 5:
            self.DeleteBacterium(bactID)

 
    def MoveCursor(self, iren, event=""):
#        self.vtkWidget.GetRenderWindow().HideCursor()
        x,y = self.iren.GetEventPosition()
        self.picker.Pick(x, y, 0, self.renderer)
        p = self.picker.GetPickPosition()
        self.setPickerPos(Vec3f(p))
        n = self.picker.GetPickNormal()
        self.redCone.SetPosition(p[0],p[1],p[2])
        self.PointCone(self.redCone,n[0],n[1],n[2])
        self.greenCone.SetPosition(p[0],p[1],p[2])
        self.PointCone(self.greenCone,-n[0],-n[1],-n[2])
        iren.Render()
        
        
    def StartInteraction(self, obj, event):
        """
        Lower the rendering resolution to make interaction more smooth.
        """
        self.vtkWidget.GetRenderWindow().SetDesiredUpdateRate(10)
        self._pickerVisibility(False)
    
    def EndInteraction(self, obj, event):
        """
        When interaction ends, the requested frame rate is decreased to
        normal levels. This causes a full resolution render to occur.
        """
        self.vtkWidget.GetRenderWindow().SetDesiredUpdateRate(0.001)
        self._pickerVisibility(True)
        
    def ClipVolumeRender(self, obj, event):
        obj.GetPlanes(self.planes)
        self.imageLayer[self.CISID].VolumeMapper.SetClippingPlanes(self.planes)
        
    
    # UTILITY METHODS
    def PointCone(self, actor, nx, ny, nz):
        actor.SetOrientation(0.0, 0.0, 0.0)
        n = math.sqrt(nx**2 + ny**2 + nz**2)
        if (nx < 0.0):
            actor.RotateWXYZ(180, 0, 1, 0)
            n = -n
        actor.RotateWXYZ(180, (nx+n)*0.5, ny*0.5, nz*0.5)
        
        
        
        
        
        
        
        