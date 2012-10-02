"""  TransformationAgent processes transformations found in the transformation database.
"""

__RCSID__ = "$Id$"

import time, re, random, Queue, threading, os, datetime, pickle
from DIRAC                                                          import  S_OK, S_ERROR
from DIRAC.Core.Base.AgentModule                                    import AgentModule
from DIRAC.Core.Utilities.ThreadPool                                import ThreadPool
from DIRAC.TransformationSystem.Client.TransformationClient         import TransformationClient
from DIRAC.TransformationSystem.Agent.TransformationAgentsUtilities import TransformationAgentsUtilities
from DIRAC.DataManagementSystem.Client.ReplicaManager               import ReplicaManager

AGENT_NAME = 'Transformation/TransformationAgent'

class TransformationAgent( AgentModule, TransformationAgentsUtilities ):
  """ Usually subclass of AgentModule
  """

  def __init__( self, agentName, loadName, baseAgentName = False, properties = dict() ):
    """ c'tor

    :param self: self reference
    :param str agentName: name of agent
    :param bool baseAgentName: whatever
    :param dict properties: whatever else
    """

    #few parameters
    self.pluginLocation = self.am_getOption( 'PluginLocation',
                                             'DIRAC.TransformationSystem.Agent.TransformationPlugin' )
    self.checkCatalog = self.am_getOption( 'CheckCatalog', 'yes' )
    self.transformationStatus = self.am_getOption( 'transformationStatus', ['Active', 'Completing', 'Flush'] )
    self.maxFiles = self.am_getOption( 'MaxFiles', 5000 )
    self.transformationTypes = self.am_getOption( 'TransformationTypes', [] )

    #clients
    self.transfClient = TransformationClient( 'TransformationDB' )
    self.rm = ReplicaManager()

    #for the threading
    self.transQueue = Queue.Queue()
    self.transInQueue = []
    self.lock = threading.Lock()

    #for caching using a pickle file
    self.workDirectory = self.am_getWorkDirectory()
    self.cacheFile = os.path.join( self.workDirectory, 'ReplicaCache.pkl' )

    # Validity of the cache in days
    self.replicaCacheValidity = 2
    self.__readCache()

    self.unusedFiles = {}

  def initialize( self ):
    """ standard init
    """

    self.am_setOption( 'shifterProxy', 'ProductionManager' )

    # Get it threaded
    maxNumberOfThreads = self.am_getOption( 'maxThreadsInPool', 1 )
    threadPool = ThreadPool( maxNumberOfThreads, maxNumberOfThreads )
    self.log.info( "Multithreaded with %d threads" % maxNumberOfThreads )

    for _i in xrange( maxNumberOfThreads ):
      threadPool.generateJobAndQueueIt( self._execute )

    return S_OK()

  def execute( self ):
    """ Just puts threads in the queue
    """
    # Get the transformations to process
    res = self.getTransformations()
    if not res['OK']:
      self.__logError( "Failed to obtain transformations: %s" % ( res['Message'] ) )
      return S_OK()
    # Process the transformations
    count = 0
    for transDict in res['Value']:
      transID = long( transDict['TransformationID'] )
      if transID not in self.transInQueue:
        count += 1
        self.transInQueue.append( transID )
        self.transQueue.put( transDict )
    self.log.info( "Out of %d transformations, %d put in thread queue" % ( len( res['Value'] ), count ) )
    return S_OK()

  def getTransformations( self ):
    """ Obtain the transformations to be executed
    """
    transName = self.am_getOption( 'Transformation', 'All' )
    if transName == 'All':
      self.__logInfo( "getTransformations: Initializing general purpose agent.", method = 'getTransformations' )
      transfDict = {'Status': self.transformationStatus }
      if self.transformationTypes:
        transfDict['Type'] = self.transformationTypes
      res = self.transDB.getTransformations( transfDict, extraParams = True )
      if not res['OK']:
        self.__logError( "Failed to get transformations: %s" % res['Message'], method = 'getTransformations' )
        return res
      transformations = res['Value']
      self.__logInfo( "Obtained %d transformations to process" % len( transformations ), method = 'getTransformations' )
    else:
      self.__logInfo( "getTransformations: Initializing for transformation %s." % transName )
      res = self.transfClient.getTransformation( transName, extraParams = True )
      if not res['OK']:
        self.__logError( "Failed to get transformation: %s." % res['Message'], method = 'getTransformations' )
        return res
      transformations = [res['Value']]
    return S_OK( transformations )

  def _execute( self ):
    """ thread - does the real job: processing the transformations to be processed
    """
    while True:
      transDict = self.transQueue.get()
      try:
        transID = long( transDict['TransformationID'] )
        self.__logInfo( "Processing transformation %s." % transID, transID = transID )
        startTime = time.time()
        res = self.processTransformation( transDict )
        if not res['OK']:
          self.__logInfo( "Failed to process transformation: %s" % res['Message'], transID = transID )
        else:
          self.__logInfo( "Processed transformation in %.1f seconds" % ( time.time() - startTime ), transID = transID )
      except Exception, x:
        self.__logException( '%s' % x, transID = transID )
      finally:
        if transID in self.transInQueue:
          self.transInQueue.remove( transID )
    return S_OK()

  def processTransformation( self, transDict ):
    """ process a single transformation (in transDict)
    """

    transID = transDict['TransformationID']
    replicateOrRemove = transDict['Type'].lower() in ['replication', 'removal']

    # First get the LFNs associated to the transformation
    transFiles = self._getTransformationFiles()
    if not transFiles['OK']:
      return transFiles

    transFiles = transFiles['Value']
    lfns = [ f['LFN'] for f in transFiles ]

    # Limit the number of LFNs to be considered for replication or removal as they are treated individually
    if replicateOrRemove:
      lfns = self.__applyReduction( lfns )

    unusedFiles = len( lfns )

    # Check the data is available with replicas
    res = self.__getDataReplicas( transID, lfns, active = not replicateOrRemove )
    if not res['OK']:
      self.__logError( "Failed to get data replicas: %s" % res['Message'],
                       method = "processTransformation", transID = transID )
      return res
    dataReplicas = res['Value']

    # Get the plug-in type and create the plug-in object
    plugin = 'Standard'
    if transDict.has_key( 'Plugin' ) and transDict['Plugin']:
      plugin = transDict['Plugin']
    self.__logInfo( "Processing transformation with '%s' plug-in." % plugin,
                    method = "processTransformation", transID = transID )
    res = self.__generatePluginObject( plugin )
    if not res['OK']:
      return res
    oPlugin = res['Value']

    # Get the plug-in and set the required params
    oPlugin.setParameters( transDict )
    oPlugin.setInputData( dataReplicas )
    oPlugin.setTransformationFiles( transFiles )
    res = oPlugin.generateTasks()
    if not res['OK']:
      self.__logError( "Failed to generate tasks for transformation: %s" % res['Message'],
                       method = "processTransformation", transID = transID )
      return res
    tasks = res['Value']
    # Create the tasks
    allCreated = True
    created = 0
    for se, lfns in tasks:
      res = self.transfClient.addTaskForTransformation( transID, lfns, se )
      if not res['OK']:
        self.__logError( "Failed to add task generated by plug-in: %s." % res['Message'],
                          method = "processTransformation", transID = transID )
        allCreated = False
      else:
        created += 1
        unusedFiles -= len( lfns )
    if created:
      self.__logInfo( "Successfully created %d tasks for transformation." % created,
                      method = "processTransformation", transID = transID )
    self.unusedFiles[transID] = unusedFiles

    # If this production is to Flush
    if transDict['Status'] == 'Flush' and allCreated:
      res = self.transfClient.setTransformationParameter( transID, 'Status', 'Active' )
      if not res['OK']:
        self.__logError( "Failed to update transformation status to 'Active': %s." % res['Message'],
                         method = "processTransformation", transID = transID )
      else:
        self.__logInfo( "Updated transformation status to 'Active'.",
                        method = "processTransformation", transID = transID )
    return S_OK()

  def finalize( self ):
    """ graceful finalization
    """
    if self.transInQueue:
      self.log.info( "Wait for queue to get empty before terminating the agent (%d tasks)" % len( self.transInQueue ) )
      while self.transInQueue:
        time.sleep( 2 )
      self.log.info( "Queue is empty, terminating the agent..." )
    return S_OK()

  ######################################################################
  #
  # Internal methods used by the agent
  #

  def pluginCallback( self, transID, invalidateCache = False ):
    """ Standard plugin callback
    """
    if invalidateCache:
      self.lock.acquire()
      try:
        self.__readCache( lock = False )
        if transID in self.replicaCache:
          self.__logInfo( "Removed cached replicas for transformation" , method = 'pluginCallBack', transID = transID )
          self.replicaCache.pop( transID )
          self.__writeCache( lock = False )
      except:
        pass
      finally:
        self.lock.release()

  def _getTransformationFiles( self, transDict ):
    """ get the data replicas for a certain transID
    """

    transID = transDict['TransformationID']

    res = self.transfClient.getTransformationFiles( condDict = {'TransformationID':transID, 'Status':'Unused'} )
    if not res['OK']:
      self.__logError( "Failed to obtain input data: %s." % res['Message'],
                       method = "_getTransformationFiles", transID = transID )
      return res
    transFiles = res['Value']

    if not transFiles:
      self.__logInfo( "No 'Unused' files found for transformation.",
                      method = "_getTransformationFiles", transID = transID )
      if transDict['Status'] == 'Flush':
        res = self.transfClient.setTransformationParameter( transID, 'Status', 'Active' )
        if not res['OK']:
          self.__logError( "Failed to update transformation status to 'Active': %s." % res['Message'],
                           method = "_getTransformationFiles", transID = transID )
        else:
          self.__logInfo( "Updated transformation status to 'Active'.",
                          method = "_getTransformationFiles", transID = transID )
      return S_OK()
    #Check if something new happened
    if len( transFiles ) == self.unusedFiles.get( transID, 0 ) and transDict['Status'] != 'Flush':
      self.__logInfo( "No new 'Unused' files found for transformation.",
                      method = "_getTransformationFiles", transID = transID )
      return S_OK()

    return S_OK( transFiles )

  def __applyReduction( self, lfns ):
    """ eventually remove the number of files to be considered
    """
    if len( lfns ) <= self.maxFiles:
      firstFile = 0
    else:
      firstFile = int( random.uniform( 0, len( lfns ) - self.maxFiles ) )
    lfns = lfns[firstFile:firstFile + self.maxFiles - 1]

    return lfns

  def __generatePluginObject( self, plugin ):
    """ This simply instantiates the TransformationPlugin class with the relevant plugin name
    """
    try:
      plugModule = __import__( self.pluginLocation, globals(), locals(), ['TransformationPlugin'] )
    except ImportError, e:
      self.__logException( "Failed to import 'TransformationPlugin' %s: %s" % ( plugin, e ),
                           method = "__generatePluginObject" )
      return S_ERROR()
    try:
      plugin_o = getattr( plugModule, 'TransformationPlugin' )( '%s' % plugin,
                                                                transClient = self.transfClient,
                                                                replicaManager = self.rm )
      return S_OK( plugin_o )
    except AttributeError, e:
      self.__logException( "Failed to create %s(): %s." % ( plugin, e ), method = "__generatePluginObject" )
      return S_ERROR()
    plugin_o.setDirectory( self.workDirectory )
    plugin_o.setCallback( self.pluginCallback )

  def __getDataReplicas( self, transID, lfns, active = True ):
    """ Get the replicas for the LFNs and check their statuses. It first looks within the cache.
    """
    self.__logVerbose( "Getting replicas for %d files" % len( lfns ), method = '__getDataReplicas', transID = transID )
    self.lock.acquire()
    try:
      cachedReplicaSets = self.replicaCache.get( transID, {} )
      dataReplicas = {}
      newLFNs = []
      for crs in cachedReplicaSets:
        cachedReplicas = cachedReplicaSets[crs]
        for lfn in [lfn for lfn in lfns if lfn in cachedReplicas]:
          dataReplicas[lfn] = cachedReplicas[lfn]
        # Remove files from the cache that are not in the required list
        for lfn in [lfn for lfn in cachedReplicas if lfn not in lfns]:
          self.replicaCache[transID][crs].pop( lfn )
    except:
      pass
    finally:
      self.lock.release()
    if dataReplicas:
      self.__logVerbose( "ReplicaCache hit for %d out of %d LFNs" % ( len( dataReplicas ), len( lfns ) ),
                         method = '__getDataReplicas', transID = transID )
    newLFNs += [lfn for lfn in lfns if lfn not in dataReplicas]
    if newLFNs:
      self.__logVerbose( "Getting replicas for %d files from catalog" % len( newLFNs ),
                         method = '__getDataReplicas', transID = transID )
      res = self.__getDataReplicasRM( self, transID, newLFNs, active = active )
      if res['OK']:
        newReplicas = res['Value']
        self.lock.acquire()
        self.replicaCache.setdefault( transID, {} )[datetime.datetime.utcnow()] = newReplicas
        self.lock.release()
        dataReplicas.update( newReplicas )
      else:
        self.__logWarn( "Failed to get replicas for %d files" % len( newLFNs ), res['Message'] )
    self.__cleanCache()
    return S_OK( dataReplicas )


  def __getDataReplicasRM( self, transID, lfns, active = True ):
    """ Get the replicas for the LFNs and check their statuses, using the replica manager
    """
    startTime = time.time()
    if active:
      res = self.rm.getActiveReplicas( lfns )
    else:
      res = self.rm.getReplicas( lfns )
    if not res['OK']:
      return res
    self.__logInfo( "Replica results for %d files obtained in %.2f seconds" % ( len( lfns ), time.time() - startTime ),
                    method = "__getDataReplicasRM", transID = transID )
    # Create a dictionary containing all the file replicas
    dataReplicas = {}
    for lfn, replicaDict in res['Value']['Successful'].items():
      ses = replicaDict.keys()
      for se in ses:
        if active and re.search( 'failover', se.lower() ):
          self.__logWarn( "Ignoring failover replica for %s." % lfn, method = "__getDataReplicasRM", transID = transID )
        else:
          if not dataReplicas.has_key( lfn ):
            dataReplicas[lfn] = {}
          dataReplicas[lfn][se] = replicaDict[se]
    # Make sure that file missing from the catalog are marked in the transformation DB.
    missingLfns = []
    for lfn, reason in res['Value']['Failed'].items():
      if re.search( "No such file or directory", reason ):
        self.__logWarn( "%s not found in the catalog." % lfn, method = "__getDataReplicasRM", transID = transID )
        missingLfns.append( lfn )
    if missingLfns:
      res = self.transfClient.setFileStatusForTransformation( transID, 'MissingLFC', missingLfns )
      if not res['OK']:
        self.__logWarn( "Failed to update status of missing files: %s." % res['Message'],
                        method = "__getDataReplicasRM", transID = transID )
    if not dataReplicas:
      return S_ERROR( "No replicas obtained" )
    return S_OK( dataReplicas )

  def __cleanCache( self ):
    """ Cleans the cache
    """
    self.lock.acquire()
    try:
      timeLimit = datetime.datetime.utcnow() - datetime.timedelta( days = self.replicaCacheValidity )
      for transID in [transID for transID in self.replicaCache]:
        for updateTime in self.replicaCache[transID].copy():
          if updateTime < timeLimit or not self.replicaCache[transID][updateTime]:
            self.__logVerbose( "Clear %d cached replicas for transformation %s" % ( len( self.replicaCache[transID][updateTime] ),
                                                                                    str( transID ) ),
                              method = '__cleanCache' )
            self.replicaCache[transID].pop( updateTime )
        # Remove empty transformations
        if not self.replicaCache[transID]:
          self.replicaCache.pop( transID )
      self.__writeCache( lock = False )
    except:
      pass
    finally:
      self.lock.release()

  def __readCache( self, lock = True ):
    """ Reads from the cache
    """
    if lock:
      self.lock.acquire()
    try:
      cacheFile = open( self.cacheFile, 'r' )
      self.replicaCache = pickle.load( cacheFile )
      cacheFile.close()
      self.log.verbose( "Successfully loaded replica cache from file %s" % self.cacheFile )
    except:
      self.log.verbose( "Failed to load replica cache from file %s" % self.cacheFile )
      self.replicaCache = {}
    finally:
      if lock:
        self.lock.release()

  def __writeCache( self, lock = True ):
    """ Writes the cache
    """
    if lock:
      self.lock.acquire()
    try:
      f = open( self.cacheFile, 'w' )
      pickle.dump( self.replicaCache, f )
      f.close()
      self.log.verbose( "Successfully wrote replica cache file %s" % self.cacheFile )
    except:
      self.log.error( "Could not write replica cache file %s" % self.cacheFile )
    finally:
      if lock: self.lock.release()
