/**
 * Indexer plugin for Orthanc
 * Copyright (C) 2021-2026 Sebastien Jodogne, ICTEAM UCLouvain, Belgium
 *
 * This program is free software: you can redistribute it and/or
 * modify it under the terms of the GNU General Public License as
 * published by the Free Software Foundation, either version 3 of the
 * License, or (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful, but
 * WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
 * General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with this program. If not, see <http://www.gnu.org/licenses/>.
 **/


#include "IndexerDatabase.h"
#include "StorageArea.h"

#include "../Resources/Orthanc/Plugins/OrthancPluginCppWrapper.h"

#include <DicomFormat/DicomInstanceHasher.h>
#include <DicomFormat/DicomMap.h>
#include <Logging.h>
#include <SerializationToolbox.h>
#include <SystemToolbox.h>

#include <boost/filesystem.hpp>
#include <boost/thread.hpp>
#include <atomic>
#include <ctime>
#include <stack>

#define ORTHANC_PLUGIN_NAME  "indexer"


static std::list<std::string>        folders_;
static IndexerDatabase               database_;
static std::unique_ptr<StorageArea>  storageArea_;
static unsigned int                  intervalSeconds_;
// SSC fork: when false, the indexer skips LookupDeletedFiles(), so files that
// disappear from the filesystem are NOT removed from Orthanc's index. Required
// for cold-storage workflows where DICOM files are temporarily evicted but
// must remain findable via /tools/lookup. Defaults to true (upstream behavior).
static bool                          removeMissingFiles_ = true;

// SSC fork: on-demand scoped scan (POST/GET /indexer/scan). Lets an external caller
// register specific subtrees without editing config or restarting Orthanc.
// `scanSerializer_` guarantees the continuous monitor and the on-demand scan never
// run at the same time (ProcessFile/RestApiPost registration is effectively serial).
static boost::mutex                  scanSerializer_;

struct OnDemandScanState
{
  boost::mutex           mutex;          // guards the non-atomic fields below
  bool                   busy = false;
  bool                   stop = false;   // set on OrthancStopped to abort the worker
  boost::thread          thread;
  std::list<std::string> folders;
  bool                   force = false;
  // Atomic so ScanFolders can bump them live (per file, no lock) while GET reads them.
  std::atomic<uint64_t>  filesProcessed{0};
  std::atomic<uint64_t>  registered{0};
  std::time_t            startedAt = 0;
  std::time_t            finishedAt = 0;
};
static OnDemandScanState             onDemand_;

// SSC fork: allow-list for /indexer/scan request folders (from Indexer.ScanRoots;
// defaults to {"/dicom-data"} at init). A request folder must be at or under one of
// these, absolute, and free of ".." — so the endpoint can never scan outside the mount.
static std::list<std::string>        scanRoots_;


static bool ComputeInstanceId(std::string& instanceId,
                              const void* dicom,
                              size_t size)
{
  if (size > 0 &&
      Orthanc::DicomMap::IsDicomFile(dicom, size))
  {
    try
    {
      OrthancPlugins::OrthancString s;
      s.Assign(OrthancPluginDicomBufferToJson(OrthancPlugins::GetGlobalContext(), dicom, size,
                                              OrthancPluginDicomToJsonFormat_Short,
                                              OrthancPluginDicomToJsonFlags_None, 256));
    
      Json::Value json;
      s.ToJson(json);
    
      static const char* const PATIENT_ID = "0010,0020";
      static const char* const STUDY_INSTANCE_UID = "0020,000d";
      static const char* const SERIES_INSTANCE_UID = "0020,000e";
      static const char* const SOP_INSTANCE_UID = "0008,0018";
    
      Orthanc::DicomInstanceHasher hasher(
        json.isMember(PATIENT_ID) ? Orthanc::SerializationToolbox::ReadString(json, PATIENT_ID) : "",
        Orthanc::SerializationToolbox::ReadString(json, STUDY_INSTANCE_UID),
        Orthanc::SerializationToolbox::ReadString(json, SERIES_INSTANCE_UID),
        Orthanc::SerializationToolbox::ReadString(json, SOP_INSTANCE_UID));

      instanceId = hasher.HashInstance();
      return true;
    }
    catch (Orthanc::OrthancException&)
    {
      return false;
    }
  }
  else
  {
    return false;
  }
}



// SSC fork: returns true iff a DICOM instance was (re)registered via POST /instances.
static bool ProcessFile(const std::string& path,
                        const std::time_t time,
                        const uintmax_t size)
{
  std::string oldInstanceId;
  IndexerDatabase::FileStatus status = database_.LookupFile(oldInstanceId, path, time, size);

  bool registered = false;

  if (status == IndexerDatabase::FileStatus_New ||
      status == IndexerDatabase::FileStatus_Modified)
  {
    if (status == IndexerDatabase::FileStatus_Modified)
    {
      database_.RemoveFile(path);
    }

    std::string dicom;
    Orthanc::SystemToolbox::ReadFile(dicom, path);

    std::string instanceId;
    if (!dicom.empty() &&
        ComputeInstanceId(instanceId, dicom.c_str(), dicom.size()))
    {
      LOG(INFO) << "New DICOM file detected by the indexer plugin: " << path;

      // The following line must be *before* the "RestApiDelete()" to
      // deal with the case of having two copies of the same DICOM
      // file in the indexed folders, but with different timestamps
      database_.AddDicomInstance(path, time, size, instanceId);

      if (status == IndexerDatabase::FileStatus_Modified)
      {
        OrthancPlugins::RestApiDelete("/instances/" + oldInstanceId, false);
      }

      try
      {
        Json::Value upload;
        OrthancPlugins::RestApiPost(upload, "/instances", dicom, false);
        registered = true;  // SSC fork
      }
      catch (Orthanc::OrthancException&)
      {
      }
    }
    else
    {
      LOG(INFO) << "Skipping indexing of non-DICOM file: " << path;
      database_.AddNonDicomFile(path, time, size);

      if (status == IndexerDatabase::FileStatus_Modified)
      {
        OrthancPlugins::RestApiDelete("/instances/" + oldInstanceId, false);
      }
    }
  }

  return registered;
}


static void LookupDeletedFiles()
{
  class Visitor : public IndexerDatabase::IFileVisitor
  {
  private:
    typedef std::pair<std::string, std::string>  DeletedDicom;
    
    std::list<DeletedDicom>  deletedDicom_;
    
  public:
    virtual void VisitInstance(const std::string& path,
                               bool isDicom,
                               const std::string& instanceId) ORTHANC_OVERRIDE
    {
      if (!Orthanc::SystemToolbox::IsRegularFile(path) &&
          isDicom)
      {
        deletedDicom_.push_back(std::make_pair(path, instanceId));
      }
    }

    void ExecuteDelete()
    {
      for (std::list<DeletedDicom>::const_iterator
             it = deletedDicom_.begin(); it != deletedDicom_.end(); ++it)
      {
        const std::string& path = it->first;
        const std::string& instanceId = it->second;

        if (database_.RemoveFile(path))
        {
          OrthancPlugins::RestApiDelete("/instances/" + instanceId, false);      
        }
      }
    }
  };  

  Visitor visitor;
  database_.Apply(visitor);
  visitor.ExecuteDelete();
}


// SSC fork: single reusable DFS scan, shared by the continuous monitor and the
// on-demand /indexer/scan endpoint. Returns early if *stop becomes true. The two
// atomic counters (may be NULL) are bumped live per file so GET /indexer/scan can
// report progress mid-scan.
static void ScanFolders(const std::list<std::string>& folders,
                        const bool* stop,
                        std::atomic<uint64_t>* filesProcessed /* may be NULL */,
                        std::atomic<uint64_t>* registeredCount /* may be NULL */)
{
  std::stack<boost::filesystem::path> s;

  for (std::list<std::string>::const_iterator it = folders.begin();
       it != folders.end(); ++it)
  {
    s.push(*it);
  }

  while (!s.empty())
  {
    if (*stop)
    {
      return;
    }

    boost::filesystem::path d = s.top();
    s.pop();

    boost::filesystem::directory_iterator current;

    try
    {
      current = boost::filesystem::directory_iterator(d);
    }
    catch (boost::filesystem::filesystem_error&)
    {
      LOG(WARNING) << "Indexer plugin cannot read directory: " << d.string();
      continue;
    }

    const boost::filesystem::directory_iterator end;

    while (current != end)
    {
      try
      {
        const boost::filesystem::file_status status = boost::filesystem::status(current->path());

        switch (status.type())
        {
          case boost::filesystem::regular_file:
          case boost::filesystem::reparse_file:
            try
            {
              bool registered = ProcessFile(current->path().string(),
                                            boost::filesystem::last_write_time(current->path()),
                                            boost::filesystem::file_size(current->path()));
              if (filesProcessed != NULL)
              {
                filesProcessed->fetch_add(1);
              }
              if (registered && registeredCount != NULL)
              {
                registeredCount->fetch_add(1);
              }
            }
            catch (Orthanc::OrthancException& e)
            {
              LOG(ERROR) << e.What();
            }
            break;

          case boost::filesystem::directory_file:
            s.push(current->path());
            break;

          default:
            break;
        }
      }
      catch (boost::filesystem::filesystem_error&)
      {
      }

      ++current;
    }
  }
}


static void MonitorDirectories(bool* stop, unsigned int intervalSeconds)
{
  for (;;)
  {
    {
      // SSC fork: mutual exclusion vs an on-demand scan.
      boost::mutex::scoped_lock lock(scanSerializer_);
      ScanFolders(folders_, stop, NULL, NULL);
    }

    if (*stop)
    {
      return;
    }

    if (removeMissingFiles_)
    {
      try
      {
        LookupDeletedFiles();
      }
      catch (Orthanc::OrthancException& e)
      {
        LOG(ERROR) << e.What();
      }
    }

    for (unsigned int i = 0; i < intervalSeconds * 10; i++)
    {
      if (*stop)
      {
        return;
      }

      boost::this_thread::sleep(boost::posix_time::milliseconds(100));
    }
  }
}


static OrthancPluginErrorCode StorageCreate(const char *uuid,
                                            const void *content,
                                            int64_t size,
                                            OrthancPluginContentType type)
{
  try
  {
    std::string instanceId;
    if (type == OrthancPluginContentType_Dicom &&
        ComputeInstanceId(instanceId, content, size) &&
        database_.AddAttachment(uuid, instanceId))
    {
      // This attachment corresponds to an external DICOM file that is
      // stored in one of the indexed folders, only store a link to it
    }
    else
    {
      // This attachment must be stored in the internal storage area
      storageArea_->Create(uuid, content, size);
    }
    
    return OrthancPluginErrorCode_Success;
  }
  catch (Orthanc::OrthancException& e)
  {
    LOG(ERROR) << e.What();
    return static_cast<OrthancPluginErrorCode>(e.GetErrorCode());
  }
  catch (...)
  {
    return OrthancPluginErrorCode_InternalError;
  }
}



static bool LookupExternalDicom(std::string& externalPath,
                                const char *uuid,
                                OrthancPluginContentType type)
{
  return (type == OrthancPluginContentType_Dicom &&
          database_.LookupAttachment(externalPath, uuid));
}


static OrthancPluginErrorCode StorageReadRange(OrthancPluginMemoryBuffer64 *target,
                                               const char *uuid,
                                               OrthancPluginContentType type,
                                               uint64_t rangeStart)
{
  try
  {
    std::string externalPath;
    if (LookupExternalDicom(externalPath, uuid, type))
    {
      StorageArea::ReadRangeFromPath(target, externalPath, rangeStart);
    }
    else
    {
      storageArea_->ReadRange(target, uuid, rangeStart);
    }
    
    return OrthancPluginErrorCode_Success;
  }
  catch (Orthanc::OrthancException& e)
  {
    LOG(ERROR) << e.What();
    return static_cast<OrthancPluginErrorCode>(e.GetErrorCode());
  }
  catch (...)
  {
    return OrthancPluginErrorCode_InternalError;
  }
}


static OrthancPluginErrorCode StorageReadWhole(OrthancPluginMemoryBuffer64 *target,
                                               const char *uuid,
                                               OrthancPluginContentType type)
{
  try
  {
    std::string externalPath;
    if (LookupExternalDicom(externalPath, uuid, type))
    {
      StorageArea::ReadWholeFromPath(target, externalPath);
    }
    else
    {
      storageArea_->ReadWhole(target, uuid);
    }

    return OrthancPluginErrorCode_Success;
  }
  catch (Orthanc::OrthancException& e)
  {
    LOG(ERROR) << e.What();
    return static_cast<OrthancPluginErrorCode>(e.GetErrorCode());
  }
  catch (...)
  {
    return OrthancPluginErrorCode_InternalError;
  }
}


static OrthancPluginErrorCode StorageRemove(const char *uuid,
                                            OrthancPluginContentType type)
{
  try
  {
    std::string externalPath;
    if (LookupExternalDicom(externalPath, uuid, type))
    {
      database_.RemoveAttachment(uuid);
    }
    else
    {
      database_.RemoveAttachment(uuid);
      storageArea_->RemoveAttachment(uuid);
    }
    
    return OrthancPluginErrorCode_Success;
  }
  catch (Orthanc::OrthancException& e)
  {
    LOG(ERROR) << e.What();
    return static_cast<OrthancPluginErrorCode>(e.GetErrorCode());
  }
  catch (...)
  {
    return OrthancPluginErrorCode_InternalError;
  }
}


// SSC fork: reject request folders that are not absolute, contain "..", or fall
// outside the configured scan roots (default {"/dicom-data"}). Returns the
// normalized path via `out` when allowed.
static bool IsAllowedScanFolder(const std::string& folder, std::string& out)
{
  boost::filesystem::path p(folder);
  if (!p.is_absolute())
  {
    return false;
  }

  // lexically_normal() resolves any real ".." traversal; reject only a genuine
  // ".." path *component* (not ".." inside a legitimate name like "RVA.."). The
  // under-a-scan-root check below is the actual boundary: an escaped path would no
  // longer be under the root.
  p = p.lexically_normal();
  for (boost::filesystem::path::iterator c = p.begin(); c != p.end(); ++c)
  {
    if (c->string() == "..")
    {
      return false;
    }
  }
  const std::string norm = p.generic_string();

  for (std::list<std::string>::const_iterator it = scanRoots_.begin();
       it != scanRoots_.end(); ++it)
  {
    const std::string root = *it;
    if (norm == root || norm.compare(0, root.size() + 1, root + "/") == 0)
    {
      out = norm;
      return true;
    }
  }
  return false;
}


// SSC fork: on-demand scan worker. Serializes against the continuous monitor via
// scanSerializer_; optionally purges the target folders' index rows first (Force).
static void RunOnDemandScan(std::list<std::string> folders, bool force)
{
  boost::mutex::scoped_lock serialize(scanSerializer_);  // wait out any in-flight monitor cycle
  try
  {
    if (force)
    {
      for (std::list<std::string>::const_iterator it = folders.begin();
           it != folders.end(); ++it)
      {
        database_.RemoveFilesUnderPrefix(*it);
      }
    }
    // Counters live on onDemand_ (atomic) so GET /indexer/scan reports live progress.
    ScanFolders(folders, &onDemand_.stop, &onDemand_.filesProcessed, &onDemand_.registered);
  }
  catch (Orthanc::OrthancException& e)
  {
    LOG(ERROR) << "On-demand indexer scan failed: " << e.What();
  }
  catch (...)
  {
    LOG(ERROR) << "On-demand indexer scan failed (native exception)";
  }

  boost::mutex::scoped_lock lock(onDemand_.mutex);
  onDemand_.finishedAt = std::time(NULL);
  onDemand_.busy = false;
  LOG(WARNING) << "On-demand indexer scan finished: files=" << onDemand_.filesProcessed.load()
               << " registered=" << onDemand_.registered.load();
}


// SSC fork: POST /indexer/scan {"Folders":[...],"Force":bool} starts an async scoped
// scan (200 {"status":"started"}, 409 if one is already running). GET /indexer/scan
// reports status so the caller can poll until busy=false.
static void ScanRestCallback(OrthancPluginRestOutput* output,
                             const char* /*url*/,
                             const OrthancPluginHttpRequest* request)
{
  if (request->method == OrthancPluginHttpMethod_Get)
  {
    Json::Value out(Json::objectValue);
    boost::mutex::scoped_lock lock(onDemand_.mutex);
    out["busy"] = onDemand_.busy;
    Json::Value fs(Json::arrayValue);
    for (std::list<std::string>::const_iterator it = onDemand_.folders.begin();
         it != onDemand_.folders.end(); ++it)
    {
      fs.append(*it);
    }
    out["folders"] = fs;
    out["filesProcessed"] = Json::UInt64(onDemand_.filesProcessed.load());
    out["registered"] = Json::UInt64(onDemand_.registered.load());
    out["startedAt"] = Json::Int64(onDemand_.startedAt);
    out["finishedAt"] = Json::Int64(onDemand_.finishedAt);
    OrthancPlugins::AnswerJson(out, output);
    return;
  }

  if (request->method != OrthancPluginHttpMethod_Post)
  {
    OrthancPlugins::AnswerMethodNotAllowed(output, "GET,POST");
    return;
  }

  Json::Value body;
  if (!OrthancPlugins::ReadJson(body, request->body, request->bodySize) ||
      body.type() != Json::objectValue ||
      !body.isMember("Folders") ||
      body["Folders"].type() != Json::arrayValue ||
      body["Folders"].empty())
  {
    OrthancPlugins::AnswerHttpError(400, output);
    return;
  }

  const bool force = (body.isMember("Force") &&
                      body["Force"].isBool() &&
                      body["Force"].asBool());

  std::list<std::string> folders;
  for (Json::ArrayIndex i = 0; i < body["Folders"].size(); i++)
  {
    const Json::Value& v = body["Folders"][i];
    std::string norm;
    if (v.type() != Json::stringValue ||
        !IsAllowedScanFolder(v.asString(), norm))
    {
      OrthancPlugins::AnswerHttpError(403, output);
      return;
    }
    folders.push_back(norm);
  }

  boost::mutex::scoped_lock lock(onDemand_.mutex);
  if (onDemand_.busy)
  {
    OrthancPlugins::AnswerHttpError(409, output);
    return;
  }
  if (onDemand_.thread.joinable())
  {
    onDemand_.thread.join();  // reap the previous finished thread before reassigning
  }

  onDemand_.busy = true;
  onDemand_.stop = false;
  onDemand_.folders = folders;
  onDemand_.force = force;
  onDemand_.filesProcessed.store(0);
  onDemand_.registered.store(0);
  onDemand_.startedAt = std::time(NULL);
  onDemand_.finishedAt = 0;
  onDemand_.thread = boost::thread(RunOnDemandScan, folders, force);

  LOG(WARNING) << "On-demand indexer scan started (" << folders.size()
               << " folder(s), force=" << force << ")";
  Json::Value ok(Json::objectValue);
  ok["status"] = "started";
  OrthancPlugins::AnswerJson(ok, output);
}


static OrthancPluginErrorCode OnChangeCallback(OrthancPluginChangeType changeType,
                                               OrthancPluginResourceType resourceType,
                                               const char* resourceId)
{
  static bool stop_;
  static boost::thread thread_;

  switch (changeType)
  {
    case OrthancPluginChangeType_OrthancStarted:
      stop_ = false;
      thread_ = boost::thread(MonitorDirectories, &stop_, intervalSeconds_);
      break;

    case OrthancPluginChangeType_OrthancStopped:
      stop_ = true;
      {
        // SSC fork: also abort + join the on-demand scan worker (if any). Signal
        // both stops before joining so a worker blocked on scanSerializer_ behind
        // the monitor returns as soon as it acquires the lock.
        boost::mutex::scoped_lock lock(onDemand_.mutex);
        onDemand_.stop = true;
      }
      if (thread_.joinable())
      {
        thread_.join();  // frees scanSerializer_
      }
      if (onDemand_.thread.joinable())
      {
        onDemand_.thread.join();
      }

      break;

    default:
      break;
  }

  return OrthancPluginErrorCode_Success;
}
      

extern "C"
{
  ORTHANC_PLUGINS_API int32_t OrthancPluginInitialize(OrthancPluginContext* context)
  {
    OrthancPlugins::SetGlobalContext(context);
    Orthanc::Logging::InitializePluginContext(context);
    Orthanc::Logging::EnableInfoLevel(true);

    /* Check the version of the Orthanc core */
    if (OrthancPluginCheckVersion(context) == 0)
    {
      OrthancPlugins::ReportMinimalOrthancVersion(ORTHANC_PLUGINS_MINIMAL_MAJOR_NUMBER,
                                                  ORTHANC_PLUGINS_MINIMAL_MINOR_NUMBER,
                                                  ORTHANC_PLUGINS_MINIMAL_REVISION_NUMBER);
      return -1;
    }

    OrthancPlugins::SetDescription(ORTHANC_PLUGIN_NAME, "Synchronize Orthanc with directories containing DICOM files.");

    OrthancPlugins::OrthancConfiguration configuration;

    OrthancPlugins::OrthancConfiguration indexer;
    configuration.GetSection(indexer, "Indexer");

    bool enabled = indexer.GetBooleanValue("Enable", false);
    if (enabled)
    {
      try
      {
        static const char* const DATABASE = "Database";
        static const char* const FOLDERS = "Folders";
        static const char* const INDEX_DIRECTORY = "IndexDirectory";
        static const char* const ORTHANC_STORAGE = "OrthancStorage";
        static const char* const STORAGE_DIRECTORY = "StorageDirectory";
        static const char* const INTERVAL = "Interval";
        static const char* const REMOVE_MISSING_FILES = "RemoveMissingFiles";
        static const char* const SCAN_ROOTS = "ScanRoots";  // SSC fork

        intervalSeconds_ = indexer.GetUnsignedIntegerValue(INTERVAL, 10 /* 10 seconds by default */);
        removeMissingFiles_ = indexer.GetBooleanValue(REMOVE_MISSING_FILES, true /* backward-compatible default */);
        if (!removeMissingFiles_)
        {
          LOG(WARNING) << "Indexer plugin: RemoveMissingFiles=false — files missing from disk "
                       << "will be KEPT in Orthanc's index (cold-storage mode)";
        }

        // SSC fork: empty/absent Folders is now valid — the continuous monitor then
        // scans nothing and POST /indexer/scan is the trigger. (Upstream threw here.)
        if (!indexer.LookupListOfStrings(folders_, FOLDERS, true))
        {
          folders_.clear();
        }

        if (folders_.empty())
        {
          LOG(WARNING) << "Indexer plugin: no static 'Folders' configured — continuous "
                       << "monitor idle; use POST /indexer/scan to trigger scoped scans";
        }
        for (std::list<std::string>::const_iterator it = folders_.begin();
             it != folders_.end(); ++it)
        {
          LOG(WARNING) << "The Indexer plugin will monitor the content of folder: " << *it;
        }

        // SSC fork: allow-list for /indexer/scan. Defaults to {"/dicom-data"} (the
        // container mount) so scan requests can never reach outside it.
        if (!indexer.LookupListOfStrings(scanRoots_, SCAN_ROOTS, true) ||
            scanRoots_.empty())
        {
          scanRoots_.clear();
          scanRoots_.push_back("/dicom-data");
        }
        for (std::list<std::string>::const_iterator it = scanRoots_.begin();
             it != scanRoots_.end(); ++it)
        {
          LOG(WARNING) << "The Indexer plugin will accept on-demand scans under: " << *it;
        }

        std::string path;
        if (!indexer.LookupStringValue(path, DATABASE))
        {
          std::string folder;
          if (!configuration.LookupStringValue(folder, INDEX_DIRECTORY))
          {
            folder = configuration.GetStringValue(STORAGE_DIRECTORY, ORTHANC_STORAGE);
          }

          Orthanc::SystemToolbox::MakeDirectory(folder);
          path = (boost::filesystem::path(folder) / "indexer-plugin.db").string();
        }
        
        LOG(WARNING) << "Path to the database of the Indexer plugin: " << path;
        database_.Open(path);

        storageArea_.reset(new StorageArea(configuration.GetStringValue(STORAGE_DIRECTORY, ORTHANC_STORAGE)));
      }
      catch (Orthanc::OrthancException& e)
      {
        return -1;
      }
      catch (...)
      {
        LOG(ERROR) << "Native exception while initializing the plugin";
        return -1;
      }

      OrthancPluginRegisterOnChangeCallback(context, OnChangeCallback);
      OrthancPluginRegisterStorageArea2(context, StorageCreate, StorageReadWhole, StorageReadRange, StorageRemove);

      // SSC fork: on-demand scoped scan endpoint (GET status + POST trigger).
      OrthancPlugins::RegisterRestCallback<ScanRestCallback>("/indexer/scan", false);
    }
    else
    {
      OrthancPlugins::LogWarning("OrthancIndexer is disabled");
    }

    return 0;
  }


  ORTHANC_PLUGINS_API void OrthancPluginFinalize()
  {
    OrthancPlugins::LogWarning("Folder indexer plugin is finalizing");
  }


  ORTHANC_PLUGINS_API const char* OrthancPluginGetName()
  {
    return ORTHANC_PLUGIN_NAME;
  }


  ORTHANC_PLUGINS_API const char* OrthancPluginGetVersion()
  {
    return ORTHANC_PLUGIN_VERSION;
  }
}
